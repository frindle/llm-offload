#!/bin/bash
# PostToolUse hook on Edit|Write (and Bash, via the file list below): when an agent
# edits a dispatch-pipeline tool in ~/bin, tell it to run the end-to-end canary.
#
# WHY (2026-10-06): the pipeline kept breaking at the SEAMS between tools on live
# runs, because each fix was tested only in its own file. ~/bin/pipeline-canary.py
# drives a real 3-slice plan through every tool against stub models in ~2.5 min;
# `pipeline-canary.py --status` reports SKIPPED while any pipeline file differs
# from the last green run. A nudge, not a block.
input="$(cat)"
path="$(printf '%s' "$input" | jq -r '.tool_input.file_path // .tool_response.filePath // empty' 2>/dev/null)"
[ -n "$path" ] || exit 0
case "$path" in
  "$HOME"/bin/*) ;;
  *) exit 0 ;;
esac
base="$(basename "$path")"
case "$base" in
  *.bak*|*.orig|*.tmp) exit 0 ;;
esac
# The file list is DERIVED (hardening design 4.5): PIPELINE_FILES plus the union of every seam's
# recorded dependencies, from `pipeline-canary.py --list-files`. If that fails (broken canary,
# python missing) fall back to the static list below so the nudge never silently disappears.
files="$(python3 "$HOME/bin/pipeline-canary.py" --list-files 2>/dev/null)"
if [ -n "$files" ]; then
  printf '%s\n' "$files" | grep -qxF -- "$base" || exit 0
else
  case "$base" in
    ollama-queue.py|ollama-worker.py|darkbloom_chat.py|mini-spec|mini-validate|mini-submit|mini-triage|\
    gate-on-complete.py|dispatch-self-heal.py|code-review-agent.py|handoff-emit.py|\
    escalation_index_janitor.py|dispatch-escalation-watcher.py|verify-relevance.py|\
    dispatch-ack-reconcile.py|pipeline-canary.py|bloom_control.py|model_profile.py|model_profiles.yaml|\
    worker_local_guards.py|ollama-queue-api.py|harness_admission.py|dispatch_harness_gates.py|\
    fixed_lane.py|worker_budget.py|scope_creep.py|cpu_lane.py|lane_plan.py|vendor_contract.py|\
    conformance_log.py|canary-staleness-check.py|test-noreread.py|test-canary-depmap.py|\
    test-canary-staleness-alarm.py) ;;
    *) exit 0 ;;
  esac
fi
# --affected in the background (design 4.7): re-prove only the seams this edit touched. OPT-IN until
# the first full parallel --prove has recorded dependency maps (until then every seam looks stale and
# it would try to re-prove the lot): `touch ~/.ollama-dispatch/canary/affected.autorun`, or
# CANARY_AFFECTED_AUTORUN=1. --max-reprove 40 leaves a stampede to the nightly --prove; one instance
# at a time (flock), CPU only, niced.
if [ "${CANARY_AFFECTED_AUTORUN:-}" = "1" ] || [ -e "$HOME/.ollama-dispatch/canary/affected.autorun" ]; then
  if [ "${CANARY_AFFECTED_AUTORUN:-}" != "0" ]; then
    mkdir -p "$HOME/bin/logs" "$HOME/.ollama-dispatch/canary" 2>/dev/null
    touch "$HOME/.ollama-dispatch/canary/affected.dirty" 2>/dev/null
    ( nohup python3 "$HOME/bin/pipeline-canary.py" --affected --max-reprove 40 \
        >> "$HOME/bin/logs/pipeline-canary-affected.log" 2>&1 < /dev/null & ) >/dev/null 2>&1
  fi
fi
jq -n --arg path "$path" '{
  hookSpecificOutput: {
    hookEventName: "PostToolUse",
    additionalContext: ("PIPELINE CANARY: you edited dispatch-pipeline file " + $path + ". Before calling the change done, run: python3 ~/bin/pipeline-canary.py   (about 3-6 min, fully sandboxed: stub models, temp HOME, no real queue/GPU). It must end PIPELINE_CANARY_OK. If it fails, fix the root cause; do not skip it. Until a green run, `pipeline-canary.py --status` and the session-start panel report PIPELINE E2E: SKIPPED.")
  }
}'
