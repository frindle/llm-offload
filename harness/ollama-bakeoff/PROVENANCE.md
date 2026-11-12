# Provenance

Records changes to pinned baselines and other facts a later reader needs in order to
interpret results rows correctly. Append-only; do not rewrite entries.

## Roster decisions

> **MFDoom/deepseek-r1-tool-calling:14b — exclusion unratified, disposition by probe (2026-08-24, Fable-ruled).** Present in the v1 roster (`bakeoff-driver.sh:42`) and v6 roster (`bakeoff-v6-macstudio.sh:73`); produced 4 rows across v5/v6.1 (all failed verify); absent from v7/v8 with no mention in the v8 pre-registration. This removal was never ratified. It is also artifact-confounded: v1 harness Bug 1 ("namespaced-model manifest path — MFDoom never loaded, 0s") hit the only namespaced model in the program's history, the same defect class fixed in `bakeoff-v8-stage.py` on 2026-08-24 (`local_models()` walked only the `library` namespace), and the local copy is incomplete (manifest, no blobs) — consistent with failed staging rather than deliberate retirement. Its 4 failed rows are not admissible merit evidence. Disposition: re-pull to Studio local, stage via the namespace-aware stage.py (blob hash-verify must pass — this staging is the regression test for the fix), then one multi-turn probe appended after devstral. Probe PASS → appended to the audition queue last, full calibration-first treatment. Probe FAIL → retired NO-GO, pre-ratified by Fable for that branch. Precedent: roster removals require explicit sign-off and a recorded rationale; silent drops are treated as unratified.

## Baseline provenance — debug cell re-point (2026-08-24)

All v8 debug rows were measured against plex-automation `bc08ae2` (branch
`bakeoff-v8-debug-baseline`). That branch's history leaked the planted defect (commit
`3f5e4ff`) to any model running git archaeology, so for v9 the identical TREE
(`git rev-parse bc08ae2^{tree}` = `42182bf77ba2241720071f808c763827f0fe3d05`) was
re-imported as a single-commit, no-remote repo (`plex-automation-debugcell`, commit
`33d4735` / `33d4735ac575b590bddf7770da6f474105b99ab6`, message "Import project").
Tree-hash equality is asserted by the scrub script. v8 and v9 debug rows are
byte-identical in task content; the baseline SHA change is provenance-only and implies
no change in difficulty or scoring. The old branch is archived at
`bakeoff-private-archive/bakeoff-v8-debug-baseline.bundle` and retired from the public
repo.
