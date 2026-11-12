# TASK: chain-2step step 2/2: `milesToKm`

## Confirmed defect (observed, not suspected)

`milesToKm(100)` returns `160`; the exact factor gives `160.9344`.

## Entry point

`src/units.js` `milesToKm`.

## Required change

This step only: `milesToKm(mi)` returns `mi * 1.609344`. `kmToMiles` was fixed in step 1 and must keep working.

Behaviour that must NOT change:
- `milesToKm(0)` stays `0`
- `kmToMiles(100)` stays `62.1371`

## Must contain

- `1.609344`

## Scope

Only edit `src/units.js`.
