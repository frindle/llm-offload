# TASK: plex-tiering-demote-block

## Confirmed defect (observed, not suspected)

A tiering pass that planned at least one promote can still go on to demote. `Runner.run_pass` starts the promote workers before it asks `Runner._demote_block` whether demotes may run. If a promote finishes fast, `promote_todo` and `promote_running` are both empty by the time the question is asked, so the "a promote is pending" block does not apply and the same pass demotes. It shows up as a flaky `test_no_demote_while_a_promote_is_pending`.

## Entry point

`tiering/offload/runner.py`: `Runner._demote_block` and its call in `Runner.run_pass`.

## Required change

A pass that planned any promote must block its demotes with the reason "a promote is pending", even when those promotes have already finished. `_demote_block` needs to learn whether the pass planned a promote (an optional argument, default meaning no), and `run_pass` must pass that in. Callers that do not pass it behave as before.

## Scope

Only edit `tiering/offload/runner.py`. Do not edit the tests.
