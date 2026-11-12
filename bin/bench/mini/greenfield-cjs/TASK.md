# TASK: greenfield-cjs

## Confirmed defect (observed, not suspected)

`parseCards` in `src/cards.js` is a seeded stub that throws `not implemented: parseCards`. It must return the card numbers found in `text`.

## Entry point

`src/cards.js` `parseCards`.

## Required change

`parseCards(text)` returns an array of the 16-digit card numbers in `text`, in order, as strings of digits only. Numbers may be written as four groups of four separated by single spaces or dashes, or as 16 digits in a row. Text with no card number returns `[]`.

Signature: parseCards(text)

Behaviour that must NOT change:
- the export name `parseCards` stays

## Must contain

- `16`

## Scope

Only edit `src/cards.js`.
