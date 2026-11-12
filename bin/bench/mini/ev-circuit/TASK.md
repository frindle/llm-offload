# TASK: ev-circuit

## Confirmed defect (observed, not suspected)

`max_charge_amps(40, 10)` returns `30`; a continuous EV load may use only 80% of the breaker rating, so it must return `22` (int(40 * 0.8) - 10). A `max_charge_amps(20, 30)` call returns `-10` instead of `0`.

## Entry point

`src/circuit.py` `max_charge_amps`.

## Required change

`max_charge_amps(breaker_amps, other_load_amps)` returns `int(breaker_amps * 0.8) - other_load_amps`, never below `0` and never above `48`.

Behaviour that must NOT change:
- `describe(16)` stays `"16A"`

## Must contain

- `breaker_amps * 0.8`

## Scope

Only edit `src/circuit.py`.
