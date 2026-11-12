# TASK: greenfield-esm

## Confirmed defect (observed, not suspected)

`slugify` in `src/slug.js` is a seeded stub that throws `not implemented: slugify`. It must turn a title into a URL slug.

## Entry point

`src/slug.js` `slugify`.

## Required change

`slugify(title, maxLen)` lowercases `title`, replaces every run of non-alphanumeric characters with a single `-`, trims leading and trailing `-`, then cuts the result to at most `maxLen` characters (no cut when `maxLen` is undefined) without leaving a trailing `-`.

Signature: slugify(title, maxLen)

Behaviour that must NOT change:
- the named export `slugify` stays

## Must contain

- `toLowerCase`

## Scope

Only edit `src/slug.js`.
