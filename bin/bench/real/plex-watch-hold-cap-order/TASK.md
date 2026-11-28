# TASK: plex-watch-hold-cap-order

## Confirmed defect (observed, not suspected)

`watch_hold.ready(entry, seen, sessions, now, cfg)` decides whether a held download may be imported. After `WATCH_HOLD_MAX_HOURS` since the download finished it returns `(True, 'max_hours')` even while a viewer has the old file paused or stopped it a few minutes ago, so a viewer who presses play again can find the file gone. Playing is already always held.

## Entry point

`watch_hold.py` `ready`.

## Required change

The max-hours cap must only skip the "fresh snapshot" and "every resume point mapped" conditions. A paused or recently streamed item (streamed now, or within the quiet period) must always hold, and it returns `(False, 'recent')`, before the cap is looked at. Once nothing has streamed for the quiet period, the cap releases as before. Update the docstring to match.

## Scope

Only edit `watch_hold.py`. Do not edit the tests.
