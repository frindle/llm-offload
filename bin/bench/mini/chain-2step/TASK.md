# TASK: chain-2step step 1/2: `kmToMiles`

## Confirmed defect (observed, not suspected)

`kmToMiles(100)` returns `62`; the exact factor gives `62.1371`.

## Entry point

`src/units.js` `kmToMiles`.

## Required change

This step only: `kmToMiles(km)` returns `km * 0.621371`. `milesToKm` is a separate step; do not change it here.

Behaviour that must NOT change:
- `kmToMiles(0)` stays `0`
- `milesToKm` is untouched in this step

## Must contain

- `0.621371`

## Scope

Only edit `src/units.js`.
