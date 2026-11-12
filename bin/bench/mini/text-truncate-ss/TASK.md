# TASK: text-truncate

## Confirmed defect (observed, not suspected)

`truncate_words("a b c d", 3)` returns `"a b"` instead of `"a b c"`. Reproduced by calling the function directly; it returns one word too few for every positive `n` because the slice on the last line is off by one.

## Entry point

`src/text.py` `truncate_words`.

## Required change

Fix `truncate_words(s, n)` so it returns exactly the first `n` whitespace-separated words of `s`, joined by single spaces. If `n <= 0` or `s` has no words, return `""`.

Behaviour that must NOT change:
- `truncate_words("a b c", 5)` still returns `"a b c"`
- `truncate_words("", 3)` still returns `""`
- `shout` is untouched

## Must contain

- `" ".join(words[:n])`

## Scope

Only edit `src/text.py`.
