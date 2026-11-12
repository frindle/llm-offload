# Review: bug-awscp.sh

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `bug-awscp.sh:10 (removed)` | Replacing `aws s3 sync` with `aws s3 cp --recursive` removes the `--delete` functionality, meaning f |
| 2 | MEDIUM | `bug-awscp.sh:7 (removed)` | `aws s3 sync` was changed to `aws s3 cp` -- a different subcommand of the same external command. |

## Defects

### [HIGH] Replacing `aws s3 sync` with `aws s3 cp --recursive` removes the `--delete` functionality, meaning files removed from the local `$BUILD_DIR` will persist in the S3 bucket.
`bug-awscp.sh:10 (removed)`

```
aws s3 sync "$BUILD_DIR" "$BUCKET" --delete
```
**How it fails:** A file is removed from the local `$BUILD_DIR` directory; after running the script, the file remains in the S3 bucket instead of being deleted.
**Verified trigger:** A file exists in the S3 bucket but is absent from the local `$BUILD_DIR` directory. After running the script, the file remains in the S3 bucket.

## Uncertain — needs a human

### [MEDIUM] `aws s3 sync` was changed to `aws s3 cp` -- a different subcommand of the same external command.
`bug-awscp.sh:7 (removed)`

```
aws s3 sync "$BUILD_DIR" "$BUCKET" --delete
```
**How it fails:** Whether `aws s3 cp` returns the same thing as `aws s3 sync` cannot be determined from this diff; it depends on aws's own behaviour. If the two differ, every caller of this code silently gets different data while the change is described as behaviour-preserving. Confirm against aws's documentation.

## How this was produced

- Model `gemma-4-26b-qat-4bit`, num_ctx 65,536, think=on — 2 calls, 34s of model time
- Wall clock 34s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*