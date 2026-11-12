# TASK: pa-supersede

## Confirmed defect (observed, not suspected)

`supersedePending([{id:1,key:'k',status:'pending'}], {id:2,key:'k',status:'pending'})` returns both actions still pending; the older one must become `superseded`.

## Entry point

`src/actions.js` `supersedePending`.

## Required change

`supersedePending(actions, incoming)` returns a NEW array: every earlier action with the same `key` as `incoming` and status `pending` becomes `{...a, status: 'superseded'}`; other actions are unchanged; `incoming` is appended last. The input array and its objects are not mutated. A falsy `incoming` returns a copy of `actions`.

Behaviour that must NOT change:
- `pendingCount` is untouched
- `done` actions with the same key stay `done`

## Must contain

- `'superseded'`

## Scope

Only edit `src/actions.js`.
