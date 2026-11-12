# TASK: browser-helper

## Confirmed defect (observed, not suspected)

`parsePrice('$1,234.50')` returns `1` (it stops at the thousands comma); it must return `1234.5`. `parsePrice('USD 9')` correctly returns `9`.

## Entry point

`src/price.js` `parsePrice`.

## Required change

`parsePrice(text)` finds the first number in `text`, ignoring thousands commas, and returns it as a number: `'$1,234.50'` -> `1234.5`, `'12'` -> `12`. Text without digits returns `null`. The helper stays free of node APIs.

Behaviour that must NOT change:
- `parsePrice('USD 9')` stays `9`
- `parsePrice('n/a')` stays `null`
- the scraper module that calls `parsePrice` is not edited

## Must contain

- `replace(/,/g`

## Scope

Only edit `src/price.js`.
