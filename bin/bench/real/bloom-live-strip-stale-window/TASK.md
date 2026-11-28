# TASK: bloom-live-strip-stale-window

## Confirmed defect (observed, not suspected)

The accounting live strip marks the Mac as stale 20 minutes (`STALE_S = 1200`) after its newest power minute. But power minutes are spooled every 15 minutes and have been seen arriving at 21 minutes under launchd interval drift, so the strip flashes stale for a normal late batch. A batch that is up to about 21 minutes old must not be stale; a Mac with nothing newer than 2000 seconds must still be stale.

## Entry point

`ingest/bloom_accounting_live.py`, the stale threshold constant, and the comment above it.

## Required change

Raise the stale threshold to 30 minutes, which matches the machine-silent alert's window (a whole spool cycle missed, not a late one), and update the comment above it to say why. Nothing else changes.

## Scope

Only edit `ingest/bloom_accounting_live.py`. Do not edit the tests.
