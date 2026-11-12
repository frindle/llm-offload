# Review: bug-awscp.sh

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `bug-awscp.sh:10` | Replacing `aws s3 sync` with `aws s3 cp` removes the automatic deletion of stale objects in the S3 b |
| 2 | MEDIUM | `bug-awscp.sh:7 (removed)` | `aws s3 sync` was changed to `aws s3 cp` -- a different subcommand of the same external command. |

## Defects

### [HIGH] Replacing `aws s3 sync` with `aws s3 cp` removes the automatic deletion of stale objects in the S3 bucket that no longer exist locally.
`bug-awscp.sh:10`

```
aws s3 cp "$BUILD_DIR" "$BUCKET" --recursive
```
**How it fails:** A file is removed from the local `./dist` directory (e.g., `old-config.json` is deleted). The script runs `publish_build`. The old file `old-config.json` remains in the S3 bucket because `aws s3 cp` only uploads new/changed files and does not delete remote files. The bucket state diverges from the local build, potentially serving stale or removed content to users.
**Verified trigger:** Local directory ./dist contains files {A, B}. S3 bucket contains {A, B, C}. Old code: `aws s3 sync` uploads A, B and deletes C. Result: Bucket has {A, B}. New code: `aws s3 cp` uploads A, B. Result: Bucket has {A, B, C}. The file C remains in the bucket.

## Uncertain — needs a human

### [MEDIUM] `aws s3 sync` was changed to `aws s3 cp` -- a different subcommand of the same external command.
`bug-awscp.sh:7 (removed)`

```
aws s3 sync "$BUILD_DIR" "$BUCKET" --delete
```
**How it fails:** Whether `aws s3 cp` returns the same thing as `aws s3 sync` cannot be determined from this diff; it depends on aws's own behaviour. If the two differ, every caller of this code silently gets different data while the change is described as behaviour-preserving. Confirm against aws's documentation.

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 65,536, think=off — 2 calls, 6s of model time
- Wall clock 7s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*