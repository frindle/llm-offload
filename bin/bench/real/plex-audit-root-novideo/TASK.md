# TASK: plex-audit-root-novideo

## Confirmed defect (observed, not suspected)

`abandoned_audit.walk(roots)` reports a folder with no video only when a parent folder with video-free children hands it up. A scan root has no parent, so when a library root contains no video at all anywhere, a video-free folder under it (for example `lib/Only (1990)` holding just a subtitle file) is never reported as `library_no_video_dir`.

## Entry point

`abandoned_audit.py` `walk`, the loop over `roots` and the inner `scan`.

## Required change

When a root as a whole has no video, report the video-free folders `scan` found under it the same way they would have been reported under a parent with video. Roots that do contain video, missing roots (still counted as errors) and every existing report row stay as they are.

## Scope

Only edit `abandoned_audit.py`. Do not edit the tests.
