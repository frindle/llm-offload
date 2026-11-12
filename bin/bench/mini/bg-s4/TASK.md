# TASK: bg-s4

## Confirmed defect (observed, not suspected)

`next_delay(10)` returns `1024` and `next_delay(-1)` returns `0.5`; the delay must be capped at `cap` (300) and a negative attempt must be treated as attempt 0. The first retry waits `base` seconds, i.e. `next_delay(0)` is `2`.

## Entry point

`src/backoff.py` `next_delay`.

## Required change

`next_delay(attempt, base=2, cap=300)` returns `min(cap, base * 2 ** max(attempt, 0))`.

Behaviour that must NOT change:
- `label(2)` stays `"retry-2"`

## Must contain

- `min(cap`

## Scope

Only edit `src/backoff.py`.
