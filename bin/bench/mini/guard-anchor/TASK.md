# TASK: guard-anchor

## Confirmed defect (observed, not suspected)

`dispatch({id:1, retries:0, queues:[{name:'slow',priority:1},{name:'fast',priority:9}]})` sends to `slow`; it must send to the queue with the HIGHEST priority (`fast`). The queue choice is the line `const best = job.queues[0];`.

## Entry point

`src/dispatch.js` `dispatch` (the `const best = job.queues[0];` line).

## Required change

Choose `best` as the queue with the greatest `priority` (first one wins a tie) instead of the first element. Everything after that line, including the dry-run and retry guards, stays as it is.

Behaviour that must NOT change:
- `dispatch(null)` still returns `{ sent: false, why: 'no job' }`
- an empty `queues` array still returns `why: 'no queue'`
- `opts.dry` still returns `why: 'dry run'`

## Must contain

- `b.priority - a.priority`

## Scope

Only edit `src/dispatch.js`.
