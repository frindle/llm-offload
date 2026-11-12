# TASK: checkout-rounding

## Confirmed defect (observed, not suspected)

`roundCents(1.005)` returns `1` and `roundCents(2.675)` returns `2.67`; the shop must round half up to the cent: `1.01` and `2.68`. `Math.floor` truncates instead of rounding.

## Entry point

`src/checkout.js` `roundCents`.

## Required change

Make `roundCents(x)` round a non-negative amount to the nearest cent, halves up, tolerating binary floating-point error (so 1.005 -> 1.01). Result is a number with at most two decimals.

Behaviour that must NOT change:
- `roundCents(1.5)` stays `1.5`
- `roundCents(0)` stays `0`
- `lineTotal(2, 3)` stays `6`

## Must contain

- `Number.EPSILON`

## Scope

Only edit `src/checkout.js`.
