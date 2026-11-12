#!/bin/bash
# v8 — the measurement round that controls host state.
#
# WHAT CHANGED FROM v7, AND WHY EACH CHANGE EXISTS
# ================================================
# v7 completed 30/30 runs and only 19 were usable. 11 were lost to the
# instrument (37%). v8 changes the instrument, not the treatment: sampling, the
# worktrees, the two tasks, the outcome taxonomy and the evidence-column
# methodology are all unchanged, because v7 established that the taxonomy
# discriminates -- all six classes occurred. The problem was that 37% of runs
# never reached the point where the taxonomy could apply.
#
# B3 (Fable) -- HOST STATE IS CONTROLLED PER RUN. This is the blocker that
#   promoted itself from refinement to hard blocker on 2026-08-23. My own v8
#   proposal argued that raising the wall clock from 1800s to 3600s would stop
#   qwen3.8 losing runs. That was FALSIFIED by direct measurement: the recovery
#   run at 3600s timed out anyway, 21 iterations in 3600s = 171 s/iteration
#   against 56 s/iteration for the identical cell on a fresh host in v7 rep 1.
#   Three times slower on an identical configuration. The wall was never the
#   binding constraint -- host degradation is, and a bigger budget only moves
#   the failure later. Measured live during that same run: swap 12354 MB of
#   14336, available memory 5857 MB of 65536, and still climbing.
#   So: restart the inference server before EVERY run, gate on available memory
#   recovering, and record host telemetry either side of every run so the
#   confound is visible in the data instead of inferred from durations.
#
# B1 -- CONTEXT IS SIZED FROM THE MODEL, NOT FROM A WISH. v7 lost 7 of its 11
#   voids to context. "64k for all" was the obvious fix and it is WRONG: it runs
#   qwen2.5-coder:14b (native 32768) and qwen3-14b-agentic (native 40960) past
#   their own spec. num_ctx = min(65536, native), verified live against
#   /api/show, and BOTH numbers go in the CSV so nobody ever compares two models
#   measured at different sizes without seeing it.
#
# B2 -- A MODEL THAT NEVER LOADED IS NOT A MODEL RESULT. Carried in the worker
#   (ollama-worker-v7.py, exit 3 = load_failed) and now surfaced as its own
#   stop_reason here. v7 row 31 died in warmup after ~2710s and appeared in the
#   CSV as exit=1, iters=0, transcript=none -- indistinguishable from a mid-run
#   crash. warmup_s and loop_s are separate columns for the same reason: 2710s
#   of failed loading must never read as 2710s of work.
#
# CEILING SPLIT -- v7 wrote `context_ceiling` for two different events. Hitting
#   the ceiling of a window we CHOSE is a fixable configuration result
#   (config_ceiling: raise num_ctx next round). Hitting the model's own maximum
#   is a property of the model (native_ceiling: nothing to raise). Same string
#   in v7, opposite conclusions.
#
# VERIFY IS TWO-PHASE -- the clamshell verify now runs the self-test, not just
#   `swift build`. It cannot be one command: preflight runs verify on the
#   PRISTINE tree, where ConfirmationBridge does not exist by construction (the
#   premise guard asserts exactly that), so a single self-test verify would
#   ABORT every clamshell run before dispatch. Preflight uses the baseline-safe
#   command; the post-run verify builds AND runs the self-test.
#
# RANDOMISED ORDER -- v7 ran the same five models in the same order every rep,
#   so the last slot was always the most degraded host and rep number was
#   confounded with position. v8 shuffles per rep, seeded by rep so a round is
#   reproducible.
#
# DELIBERATELY NOT CHANGED: sampling, worktrees, the two tasks' substance, the
# outcome taxonomy, the evidence-column methodology.
set -uo pipefail

BASE="/Users/user/Desktop/GitHub Projects"
WT_BASE="$BASE/bakeoff-build-2026-08-22"
OUTDIR="$BASE/model-buildoff-2026-08-22"
DRIVER_LOG="$OUTDIR/driver.log"
HOSTTEL="$BASE/bakeoff/harness/bakeoff-v8-hosttel.py"

WORKER="/Users/user/bin/ollama-worker-v7.py"

RUN_TAG="${RUN_TAG:-v8}"
RESULTS_CSV="$OUTDIR/results-${RUN_TAG}.csv"

# 28 columns. Wider than v7's 13 because every one of the extra 11 exists to
# answer a question v7 could not: was the host degraded, was time spent loading
# or working, was the ceiling ours or the model's, which arm was this.
[ -f "$RESULTS_CSV" ] || echo "model,backend,task,arm,rep,exit_code,verify_passed,selftest,duration_s,warmup_s,loop_s,timed_out,files_changed,iterations,num_ctx,native_ctx,stop_reason,host_ready,prompt_tokens,output_tokens,decode_s,out_tps,swap_before_mb,swap_after_mb,avail_before_mb,avail_after_mb,load_before,transcript" > "$RESULTS_CSV"

BASELINE_clamshell="8803d67"
BASELINE_resell_tracker="271aa9e"
# Debug cell. DELIBERATELY BROKEN TREE -- see the branch
# bakeoff-v8-debug-baseline in plex-automation.
BASELINE_plex_automation="33d4735"

baseline_for_repo() {
  case "$1" in
    resell-tracker) echo "$BASELINE_resell_tracker" ;;
    clamshell)      echo "$BASELINE_clamshell" ;;
    plex-automation) echo "$BASELINE_plex_automation" ;;
    *)              echo "" ;;
  esac
}

# Native context windows, read live from /api/show on 2026-08-23 rather than
# recalled. qwen2.5-coder:14b and qwen3-14b-agentic are the two that make
# "65536 for everyone" wrong -- both would have been run past their own spec.
native_ctx_for() {
  case "$1" in
    qwen3.8:27b-q8_0)        echo 262144 ;;
    qwen3-14b-agentic)       echo 40960  ;;
    deepseek-r1:32b)         echo 131072 ;;
    qwen2.5-coder:14b)       echo 32768  ;;
    qwen3-coder-next:q4_K_M) echo 262144 ;;
    qwen3-coder:30b)         echo 262144 ;;
    # Catch-up roster, 2026-08-25. Purely ADDITIVE -- no incumbent's value is
    # touched, so v9's completed rows are unaffected. Every figure below was
    # read from /api/show on the exact tag that will be dispatched, not from
    # the model card and not from the vault: Fable's preflight item 3, after
    # qwen3-coder:30b's config_ceiling showed how much a wrong window costs.
    ornith-1.5:9b)           echo 262144 ;;   # qwen35 dense, 8.95B params
    ornith-1.5:35b)          echo 262144 ;;   # qwen35moe, 35.5B params
    gpt-oss:20b)             echo 131072 ;;   # gptoss, 20.9B params
    *)                       echo 0      ;;   # 0 = unknown -> caller must abort
  esac
}

# num_ctx = min(65536, native). 65536 because it is the size that made qwen3.8's
# photo cell measurable at all (it peaks at 72% of 64k on photo); native because
# exceeding it is not a bigger window, it is an out-of-spec run.
#
# CTX_OVERRIDE re-runs a model at a LARGER window than the 65536 default, for the
# one case Fable's ruling requires it: a `config_ceiling` row is a BUG in how we
# ran it and is re-runnable, whereas a `native_ceiling` row is a real measurement
# of the model. qwen3-coder:30b stopped at config_ceiling in ALL SIX of its debug
# runs at 65536 of a native 262144, so it has never been measured on that cell.
#
# Still clamped to native: exceeding native is not a bigger window, it is an
# out-of-spec run, and the clamp is what keeps the override from silently
# becoming one. An override above native is refused loudly rather than trimmed
# quietly, because a trimmed override would produce a row labelled 131072 that
# actually ran at something else.
ctx_for() {
  local NATIVE; NATIVE=$(native_ctx_for "$1")
  if [ "$NATIVE" -eq 0 ]; then echo 0; return; fi
  if [ -n "${CTX_OVERRIDE:-}" ]; then
    if [ "$CTX_OVERRIDE" -gt "$NATIVE" ]; then
      echo "[ctx_for] CTX_OVERRIDE=$CTX_OVERRIDE exceeds native $NATIVE for $1" >&2
      echo 0; return
    fi
    echo "$CTX_OVERRIDE"; return
  fi
  if [ "$NATIVE" -lt 65536 ]; then echo "$NATIVE"; else echo 65536; fi
}

# ---------------------------------------------------------------------------
# B3: host control
# ---------------------------------------------------------------------------

# Resident size on this host, MB. Measured, not guessed -- these are the numbers
# the gate is actually predicting against.
resident_mb_for() {
  case "$1" in
    qwen3.8:27b-q8_0)        echo 30000 ;;
    qwen3-coder-next:q4_K_M) echo 51700 ;;
    deepseek-r1:32b)         echo 19900 ;;
    qwen3-14b-agentic)       echo  9300 ;;
    qwen2.5-coder:14b)       echo  9000 ;;
    qwen3-coder:30b)         echo 18600 ;;   # MoE 30.5B, ~3B active, Q4_K_M
    *)                       echo 20000 ;;   # conservative default
  esac
}

# How much available memory the host must recover before we dispatch THIS model.
#
# FABLE, second review: a flat 40%-of-physical (26214MB here) is wrong in both
# directions at once. It over-demands for qwen2.5-coder:14b (~9GB resident), so
# that model would eat a spurious 240s wait and a misleading host_ready=no on
# every run; and it under-demands for qwen3.8:27b-q8_0 (~30GB plus a 64k KV
# cache), where 26GB free is not enough to load without paging. The gate's job,
# per bakeoff-v8-hosttel.py's own docstring, is to predict whether the NEXT MODEL
# LOAD fits without paging -- which is inherently model-relative.
#
# resident x 1.2 covers the weights plus headroom; KV is added from the actual
# num_ctx, since that is the other half of what has to fit and it is exactly what
# made qwen3.8's 64k photo cell push this host into 12GB of swap.
readiness_threshold_mb() {
  local MODEL="${1:-}" CTX="${2:-32768}"
  local TOTAL; TOTAL=$(python3 "$HOSTTEL" --total)
  if [ -z "$MODEL" ]; then echo $(( TOTAL * 40 / 100 )); return; fi

  local RES; RES=$(resident_mb_for "$MODEL")
  # KV allowance, MB per 1k tokens.
  #
  # This was 2 and that was WRONG BY ~75x (Fable, 2026-08-23). 2MB/1k tokens is
  # 2KB/token; the real figure for this size class -- ~64 layers, 8 GQA KV heads,
  # head_dim 128, q8_0 K and V cache -- is 2 x 64 x 8 x 128 x 1 byte = 128KB per
  # token, i.e. ~128MB per 1k tokens, ~8.4GB at 65536. The old constant allotted
  # 128MB for that.
  #
  # Why it mattered rather than being a rounding error: the gate exists to
  # predict whether the next model load fits WITHOUT PAGING, and the cells that
  # motivated B3 at all are precisely the big models at 64k. A 75x-short KV term
  # meant host_ready=yes could be stamped on a run that was about to page --
  # certifying the exact confound the flag exists to expose. v7 corroborates the
  # real number: qwen3.8 at 64k drove 12GB into swap.
  #
  # 150 rather than 128: rough on the safe side, since head counts vary across
  # the roster and a too-generous gate costs a wait, while a too-tight one costs
  # a silently invalid row.
  local KV=$(( CTX * 150 / 1024 ))
  local NEED=$(( RES * 12 / 10 + KV ))

  # Never demand more than 85% of physical: a threshold the host cannot reach
  # even when idle would make every run wait the full 240s and flag host_ready=no
  # universally, which destroys the flag's meaning rather than protecting a run.
  local CAP=$(( TOTAL * 85 / 100 ))
  [ "$NEED" -gt "$CAP" ] && NEED="$CAP"
  echo "$NEED"
}

# Restart Ollama and WAIT FOR THE HOST TO ACTUALLY RECOVER, which is not the
# same thing and is the whole point of B3. Returns 0 if the readiness gate was
# met, 1 if we gave up waiting -- and the caller records that either way as
# host_ready, so a run dispatched onto a still-degraded host is visible in the
# data instead of silently producing another 171 s/iteration mystery.
#
# The gate is on AVAILABLE MEMORY, never on swap. macOS does not shrink swap
# files promptly when pages are freed, so a swap gate would block for minutes
# after the memory was already back. Swap is recorded, not gated on.
restart_inference_server() {
  local HOST="$1" MODEL="${2:-}" CTX="${3:-32768}"

  if [[ "$HOST" != *"localhost"* && "$HOST" != *"127.0.0.1"* ]]; then
    # We do not own a remote host's lifecycle -- Unraid's Ollama serves other
    # things. Record it honestly rather than pretending the host was controlled.
    echo "[$RUN_TAG] host-control: $HOST is remote, NOT restarting" >> "$DRIVER_LOG"
    return 1
  fi

  # STAGE THE MODEL BEFORE THE RUN, NEVER DURING ONE.
  #
  # Ollama now serves from local SSD (OLLAMA_MODELS=~/.ollama/models); Unraid is
  # the store. Copying happens here -- between tests, host idle, nothing being
  # measured -- because a 17-50GB SMB read concurrent with a run would inject
  # network, disk and page-cache contention straight into the s/iteration figure
  # that B3 exists to protect.
  #
  # This is also the RELIABILITY fix, not just a speed one. Ollama's own load
  # path over SMB hangs; a plain file copy does not. Measured today:
  # qwen3-coder-next (51.7GB) failed to finish loading inside the 900s warmup
  # budget straight off the share and killed the positive control with a
  # TimeoutError. Copy-then-load-local turns an unbounded network load into a
  # bounded copy plus a fast local load.
  if [ -n "$MODEL" ]; then
    echo "[$RUN_TAG] staging $MODEL (local SSD; Unraid is the store) ..." >> "$DRIVER_LOG"
    python3 "$BASE/bakeoff/harness/bakeoff-v8-stage.py" stage "$MODEL" \
      --keep "qwen3.8:27b-q8_0,qwen3-14b-agentic:latest,deepseek-r1:32b,qwen2.5-coder:14b,qwen3-coder-next:q4_K_M,qwen3-coder:30b" \
      >> "$DRIVER_LOG" 2>&1 || echo "[$RUN_TAG] WARNING: staging $MODEL failed -- run may load slowly or fail" >> "$DRIVER_LOG"
  fi

  echo "[$RUN_TAG] host-control: restarting ollama ..." >> "$DRIVER_LOG"
  /opt/homebrew/bin/brew services restart ollama >/dev/null 2>&1

  # Wait for the API to answer again before timing anything.
  local UP=1
  for _ in $(seq 1 30); do
    sleep 2
    if curl -s --max-time 5 "$HOST/api/tags" >/dev/null 2>&1; then UP=0; break; fi
  done
  if [ "$UP" -ne 0 ]; then
    echo "[$RUN_TAG] host-control: ollama did NOT come back within 60s" >> "$DRIVER_LOG"
    return 1
  fi

  # Then wait for memory to actually come back. Bounded: 240s, because an
  # unbounded wait would hang a 30-run round on one bad host.
  local NEED; NEED=$(readiness_threshold_mb "$MODEL" "$CTX")
  local AVAIL=0
  for _ in $(seq 1 48); do
    AVAIL=$(python3 "$HOSTTEL" --available)
    [ "$AVAIL" -ge "$NEED" ] && break
    sleep 5
  done

  if [ "$AVAIL" -ge "$NEED" ]; then
    echo "[$RUN_TAG] host-control: READY avail=${AVAIL}MB (need ${NEED}MB)" >> "$DRIVER_LOG"
    return 0
  fi
  echo "[$RUN_TAG] host-control: DEGRADED avail=${AVAIL}MB (need ${NEED}MB) -- dispatching anyway, flagged host_ready=no" >> "$DRIVER_LOG"
  return 1
}

# ---------------------------------------------------------------------------
# Experiment arms restored from v7's open questions (sections 4 and 5)
# ---------------------------------------------------------------------------

# Arm `repomap` -- the evidence-salience test. Hypothesis: exploration failure
# tracks codebase size and evidence salience, not the model's willingness to
# look. qwen3-14b-agentic is exploration-fail on photo (one file among ~40
# components in a large Next.js app) and transfer-fail on clamshell (evidence is
# one manifest plus one directory listing). If prepending a repo map collapses
# the exploration failures, this is an addressable PROMPT problem, not a model
# defect -- a much better outcome than the current framing.
#
# Generated from the worktree at dispatch time, so it can never drift from the
# tree the model is actually given.
build_repo_map() {
  local WT_DIR="$1"
  echo "Repository map (generated, for orientation -- not exhaustive):"
  echo
  ( cd "$WT_DIR" && git ls-files 2>/dev/null \
      | grep -vE '^(node_modules|\.next|dist|build|\.build)/' \
      | grep -vE '(package-lock\.json|yarn\.lock|pnpm-lock\.yaml)$' \
      | grep -vE '^app/generated/' \
      | head -400 )
}

# Arm `apisurface` -- the API-recall test. Three models invented three DIFFERENT
# wrong identifiers for the same call (P256.KeyPair, P256.SigningKey,
# P256.Signing.Signature); only qwen3.8 recovered, and only via the compiler
# error. If supplying the real surface removes the failure class, the finding
# for real dispatch work is concrete: supply the API surface, do not expect
# recall.
#
# Extracted from the SDK's own .swiftinterface rather than hand-transcribed --
# a hand-written excerpt that was subtly wrong would test our typing, not the
# model. Falls back to an explicit marker if the SDK layout changes, so a
# missing excerpt can never silently degrade this arm into the base arm.
# Brace-matched extraction, in bakeoff-v8-apisurface.py. This was awk until the
# smoke test showed the awk version bleeding twenty lines of P256.KeyAgreement
# into the excerpt and stripping the sign/verify functions out of their owning
# `extension ...PrivateKey {` scope -- which would have handed the models a
# surface with exactly the ambiguity this arm is supposed to remove. See that
# file's docstring.
build_api_surface() {
  python3 "$BASE/bakeoff/harness/bakeoff-v8-apisurface.py"
}

# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

# v8 fix #4 -- the last paragraph is new. TASK_RESELL told the model to follow
# existing patterns AND to "implement this as a real, working feature" AND asked
# which files it CREATED, while the feature already exists at baseline
# (attachments API + OrderAttachments.tsx + prisma OrderAttachment, June 2026).
# A model that found OrderAttachments.tsx and built anyway may have been OBEYING
# the instruction. That ambiguity is the cheapest confound in the round to
# remove, and removing it converts a muddled instruction into a clean
# discriminator: saying so explicitly is now the correct answer, and files=0
# with a correct report is a PASS, not a null result.
TASK_RESELL="Build a photo-upload feature for this resell-tracker web app, usable from mobile iOS devices (mobile-friendly UI, works well opened in Safari on an iPhone), that lets a user upload one or more images and match them to a specific order. This is mainly for gift card orders and coin orders/purchases -- the uploaded photos serve as a proof/record for those order types. Implement this as a real, working feature: a UI for uploading (ideally supporting camera/photo-library access on iOS), a way to associate the upload with a specific order, real storage of the uploaded images, and any necessary backend/API routes. Explore the existing codebase structure first (framework, styling conventions, API routes, database schema) and follow its existing patterns rather than inventing a new style.

If this feature already exists in some form, say so explicitly and extend the existing implementation rather than reimplementing it.

When you are done, respond with a short written summary (no further tool calls) describing exactly what you built, which files you created/changed, and any part of the feature you were not able to complete or verify."

# The self-test subcommand name is now GIVEN rather than left to the model.
# v8 verifies by RUNNING the self-test, so the harness has to know what to run;
# discovering the name by probing usage text would score naming rather than
# correctness, and a model that solved the crypto but called its subcommand
# something else would be indistinguishable from one that never wired it up.
# The project convention is already `<name>-selftest` (stream-selftest,
# window-capture-selftest, window-hide-selftest, window-at-cursor-selftest), so
# naming it costs the model nothing it was being measured on.
TASK_CLAMSHELL="Build a new Swift module for this Clamshell project called ConfirmationBridge that implements challenge-signed remote confirmation using P-256 (ECDSA). Purpose: let a privileged action on the host require an explicit signed approval from a human physically at the client, not just anyone who can reach the host. This should be a standalone module, not yet wired into the real streaming protocol.

Requirements:
1. A device can produce a signed response to a challenge using a P-256 key.
2. A correctly-signed response for a given challenge verifies successfully.
3. A replayed signature/nonce (reusing a previous valid response) must be rejected.
4. An expired challenge/nonce must be rejected -- the challenge has a limited validity window.

Write a real, synchronous self-test that exercises all three properties end to end: a valid signature verifies, a replay is rejected, and a genuinely expired nonce is rejected (actually wait for the real expiry window to elapse -- do not simulate or fake the clock). Use a SHORT validity window of a few seconds (3-5 seconds is ideal) so that waiting for a real expiry keeps the whole self-test under a minute. Wire it up as a CLI subcommand named exactly 'confirmation-bridge-selftest', consistent with the existing '*-selftest' subcommands in main.swift, so that 'swift run Clamshell confirmation-bridge-selftest' runs it and exits non-zero if any property fails. Explore the existing codebase first (check main.swift and whether a Sources/Clamshell/Auth directory already exists) before writing new code."

# DEBUG CELL (Fable item 4). The two original tasks are both single-shot
# GREENFIELD FEATURE work, in TypeScript and Swift -- the two languages local
# models are relatively weakest at. The owner's actual dispatch fleet is heavily
# PYTHON (netmon, plex-automation, the FastAPI dashcam app), and his most common
# dispatch shape is "investigate this thing that stopped working". v8 as
# originally designed would therefore have systematically underrated local models
# on the dominant LANGUAGE while saying nothing at all about the dominant SHAPE.
#
# This is written as a SYMPTOM REPORT, deliberately. It never names the function,
# the regex, or the file. It describes what a user observed, which is what a real
# dispatch looks like. Finding the cause is the measured work; a task that points
# at the defect would measure typing.
#
# The repro test already exists in the tree and fails. That is realistic rather
# than a giveaway -- "here is the bug report and here is a failing test" is
# ordinary engineering -- and it lets the model verify its own fix, which is
# precisely the self-verify behaviour the preregistration scores.
TASK_DEBUG="Something in the Plex automation is misbehaving and I need you to work out why and fix it.

The symptom: when an upgraded release comes in as a PROPER or REPACK, the old torrent it replaces is supposed to be deleted outright if the replacement came from the same release group. That is not happening consistently. For a lot of releases the old torrent is only getting soft-superseded instead, so superseded entries are piling up and sitting around far longer than they should. It seems to affect some release groups but not others, which is what is confusing me -- for plenty of releases it still works exactly as expected.

I wrote a quick reproduction for it -- test_release_group_numeric.py -- and that one fails. The older checks in the repo still pass, which is part of why I have not been able to pin it down.

Run the tests with /Users/user/bin/plex-bakeoff-venv/bin/python (the repo needs flask and requests, and the system python3 does not have them).

Investigate the codebase, work out the actual root cause, and fix it. Do not change behaviour beyond what is needed to correct the defect -- in particular, releases that work correctly today must keep working. Explore the repo first to understand how upgrades, supersede and cleanup fit together before changing anything.

When you are done, respond with a short written summary (no further tool calls) describing what the root cause was, what you changed, and anything you could not verify."

NUDGE="

Your first response must contain a tool call, not just text -- start immediately by exploring the file system."

# run_model_v8 <model> <slug> <timeout> <manual> <nudge> <backend> <host> <rep> <arm>
# ctx is no longer a parameter -- it is derived from the model. Passing it by
# hand is what allowed v7 to run two models past their native window.
run_model_v8() {
  local MODEL="$1" SLUG="$2" TMO="$3" MANUAL="$4" USE_NUDGE="$5" BACKEND="$6" HOST="$7" REP="$8" ARM="${9:-base}"
  local TAG="${RUN_TAG}-$BACKEND"

  local CTX; CTX=$(ctx_for "$MODEL")
  if [ "$CTX" -eq 0 ]; then
    echo "[$TAG] ABORT: no native context recorded for '$MODEL' -- add it to native_ctx_for()" >> "$DRIVER_LOG"
    return
  fi

  # Sampling unchanged from v6/v7 so the treatment is constant across rounds.
  local SAMPLING
  case "$MODEL" in
    deepseek-r1:*|MFDoom/*) SAMPLING=(--temperature 0.6 --top-p 0.95) ;;
    qwen*)                  SAMPLING=(--temperature 0.2 --top-p 0.95 --top-k 20) ;;
    devstral*)              SAMPLING=(--temperature 0.2 --top-p 0.95) ;;
    *)                      SAMPLING=(--temperature 0.6 --top-p 0.95) ;;
  esac
  local EXTRA=()
  [ "$MANUAL" = "yes" ] && EXTRA+=(--manual-tools)

  # qwen2.5-coder:14b's photo cell is RETIRED, not skipped by accident. It is
  # settled: ceiling x3 in v7 at 32768, and 32768 is its native maximum, so
  # there is no larger window to retry at. Running it again would spend ~30
  # minutes per rep to reproduce a known native_ceiling. The clamshell cell is
  # unaffected and still runs.
  local RUN_PHOTO="yes"
  if [ "$MODEL" = "qwen2.5-coder:14b" ]; then
    RUN_PHOTO="no"
    echo "[$TAG] photo cell RETIRED for $MODEL (settled: native_ceiling at 32768, x3 in v7)" >> "$DRIVER_LOG"
  fi

  # ONLY_TASK lets the experiment-arm driver run a single cell: the salience arm
  # is a photo-task hypothesis and the API-surface arm is a clamshell-task
  # hypothesis, so running both tasks for either would burn hours generating
  # rows that arm cannot interpret.
  local RUN_CLAM="yes" RUN_DEBUG="yes"
  case "${ONLY_TASK:-all}" in
    photo) RUN_CLAM="no"; RUN_DEBUG="no" ;;
    clam)  RUN_PHOTO="no"; RUN_DEBUG="no" ;;
    debug) RUN_PHOTO="no"; RUN_CLAM="no" ;;
  esac

  if [ "$RUN_PHOTO" = "yes" ]; then
    _one_task_v8 "$MODEL" "$SLUG" "$CTX" "$TMO" "resell-tracker" "resell-tracker-photo-upload" \
                  "npm run build" "npm run build" "$TASK_RESELL" "$USE_NUDGE" "$TAG" "$HOST" \
                  "$BACKEND" "$REP" "$ARM" "${SAMPLING[@]}" "${EXTRA[@]+"${EXTRA[@]}"}"
  fi

  # Two-phase verify. Preflight is baseline-safe (`swift build`); the post-run
  # verify builds AND runs the self-test, so "it compiles" can no longer pass
  # for "the crypto is right". v7 could not tell those apart on this task.
  if [ "$RUN_CLAM" = "yes" ]; then
    _one_task_v8 "$MODEL" "$SLUG" "$CTX" "$TMO" "clamshell" "clamshell-confirmation-bridge" \
                  "swift build" "swift build && swift run Clamshell confirmation-bridge-selftest" \
                  "$TASK_CLAMSHELL" "$USE_NUDGE" "$TAG" "$HOST" "$BACKEND" "$REP" "$ARM" \
                  "${SAMPLING[@]}" "${EXTRA[@]+"${EXTRA[@]}"}"
  fi

  # The verify RESTORES both test files from the baseline before running them.
  # Without that, a model can make the suite pass by editing the tests rather
  # than the code, and score verify_passed=yes for it -- the tests are ground
  # truth here, not part of the work. The checkout also reverts test edits before
  # the diff is archived, so evidence of gaming lives in the TRANSCRIPT; debug
  # rows must be scored for test-file mutations there.
  #
  # Debug cell. PREFLIGHT_MUST_FAIL inverts the guard: the repro has to FAIL on
  # the pristine tree, proving the planted defect is present. A shared venv is
  # used because the repo needs flask/requests and the ambient python3 has
  # neither -- learned the hard way, a worktree whose deps were never installed
  # has already wasted one dispatch on this project.
  if [ "$RUN_DEBUG" = "yes" ]; then
    local PYBIN="/Users/user/bin/plex-bakeoff-venv/bin/python"
    PREFLIGHT_MUST_FAIL=yes _one_task_v8 "$MODEL" "$SLUG" "$CTX" "$TMO" "plex-automation" "plex-release-group-debug" \
                  "$PYBIN test_release_group_numeric.py" \
                  "git checkout $BASELINE_plex_automation -- test_release_group_numeric.py test_release_group.py && $PYBIN test_release_group_numeric.py && $PYBIN test_release_group.py" \
                  "$TASK_DEBUG" "$USE_NUDGE" "$TAG" "$HOST" "$BACKEND" "$REP" "$ARM" \
                  "${SAMPLING[@]}" "${EXTRA[@]+"${EXTRA[@]}"}"
  fi
}

_one_task_v8() {
  local MODEL="$1" SLUG="$2" CTX="$3" TMO="$4" REPO="$5" TASK_NAME="$6"
  local PREFLIGHT_VERIFY="$7" RUN_VERIFY="$8" TASK_TEXT="$9" USE_NUDGE="${10}"
  local PREFLIGHT_MUST_FAIL="${PREFLIGHT_MUST_FAIL:-no}"  # debug cells invert this
  local TAG="${11}" HOST="${12}" BACKEND="${13}" REP="${14}" ARM="${15}"
  shift 15
  local ARGS=("$@")

  local NATIVE; NATIVE=$(native_ctx_for "$MODEL")
  local WT_DIR="$WT_BASE/$REPO/$SLUG"
  local STEM="$OUTDIR/$SLUG-$TASK_NAME-${TAG}-${ARM}-r${REP}"
  local LOG="$STEM.log"

  # 24-field abort row helper -- keeps every early return the same width as a
  # real row. v7 had 13-field aborts in a 13-field CSV and it was already
  # fiddly; at 24 it would be a guaranteed source of misaligned columns.
  _abort_row() {
    echo "$MODEL,$BACKEND,$TASK_NAME,$ARM,$REP,$1,na,na,0,0,0,false,0,0,$CTX,$NATIVE,$1,na,-1,-1,-1,-1,-1,-1,-1,-1,-1,none" >> "$RESULTS_CSV"
  }

  if [ ! -d "$WT_DIR" ]; then
    echo "[$TAG] ABORT (worktree missing): $MODEL / $TASK_NAME -- $WT_DIR" >> "$DRIVER_LOG"
    _abort_row "ABORT_NO_WORKTREE"; return
  fi

  local BASE_SHA; BASE_SHA=$(baseline_for_repo "$REPO")
  if [ -z "$BASE_SHA" ]; then
    echo "[$TAG] ABORT: no pinned baseline for repo '$REPO'" >> "$DRIVER_LOG"
    _abort_row "ABORT_NO_BASELINE"; return
  fi

  git -C "$WT_DIR" reset --hard "$BASE_SHA" >/dev/null 2>&1
  git -C "$WT_DIR" clean -fd >/dev/null 2>&1

  # Premise guard: refuse to dispatch a "create X" task when X already exists.
  # Only meaningful for clamshell -- the photo-upload premise is KNOWN broken
  # and that task is deliberately scored as comprehension (see TASK_RESELL).
  if [ "$REPO" = "clamshell" ] && { [ -e "$WT_DIR/Sources/ConfirmationBridge" ] || [ -e "$WT_DIR/Sources/Clamshell/Auth/ConfirmationBridge.swift" ]; }; then
    echo "[$TAG] ABORT: ConfirmationBridge already present at baseline -- task premise broken" >> "$DRIVER_LOG"
    _abort_row "ABORT_PREMISE"; return
  fi

  # Preflight polarity. For a FEATURE task the pristine tree must BUILD, so a
  # failure means the environment is broken and we must not dispatch. For a
  # DEBUG task the premise is the opposite: the repro must FAIL on the pristine
  # tree, because that is what proves the planted defect is actually present and
  # the task is not a no-op. A debug cell whose repro passes at baseline would
  # silently score every model as an instant success.
  echo "[$TAG] preflight ($( [ "$PREFLIGHT_MUST_FAIL" = "yes" ] && echo "must FAIL" || echo "must PASS" )): '$PREFLIGHT_VERIFY' on pristine $REPO/$SLUG ..." >> "$DRIVER_LOG"
  if ( cd "$WT_DIR" && eval "$PREFLIGHT_VERIFY" >/dev/null 2>&1 ); then PF_RC=0; else PF_RC=1; fi
  if [ "$PREFLIGHT_MUST_FAIL" = "yes" ]; then
    if [ "$PF_RC" -eq 0 ]; then
      echo "[$TAG] ABORT: repro PASSES on the pristine tree -- the planted defect is absent, task premise broken" >> "$DRIVER_LOG"
      _abort_row "ABORT_PREMISE_NOT_BROKEN"; return
    fi
  else
    if [ "$PF_RC" -ne 0 ]; then
      echo "[$TAG] ABORT: pristine-tree preflight FAILED -- not dispatching" >> "$DRIVER_LOG"
      _abort_row "ABORT_PREFLIGHT"; return
    fi
  fi
  # ---- VACUOUS_PASS GATE (Fable, 2026-08-25) -----------------------------
  # Measure whether the REAL verify -- not the preflight -- discriminates against
  # inaction, by running it on the pristine tree. If it PASSES there, then a model
  # that changes nothing passes it, and `verify_passed` cannot be that cell's
  # success metric.
  #
  # This is a measurement, not a reminder. The photo cell shipped two inflated
  # verdicts for exactly this reason: preflight and verify were both
  # `npm run build`, preflight had to PASS, so verify passed on an untouched tree
  # and a model that changed 0 files in 6 iterations counted as a success. Fable's
  # ruling: make the declaration mechanical, because parity was miscounted three
  # times until the auditor became a tool rather than a remembered step.
  #
  # `VACUOUS_PASS` is an INSTRUMENT failure class, not a model honesty class. A
  # model that claims done while a non-discriminating verify agrees has not
  # fabricated anything -- the instrument agreed with it. Brand the cell.
  local VERIFY_DISCRIMINATES=unknown
  if [ -n "${RUN_VERIFY:-}" ] && [ "$RUN_VERIFY" != "$PREFLIGHT_VERIFY" ]; then
    if ( cd "$WT_DIR" && eval "$RUN_VERIFY" >/dev/null 2>&1 ); then
      VERIFY_DISCRIMINATES=no
    else
      VERIFY_DISCRIMINATES=yes
    fi
  elif [ "$RUN_VERIFY" = "$PREFLIGHT_VERIFY" ] && [ "$PREFLIGHT_MUST_FAIL" != "yes" ]; then
    # Same command, and it had to pass pristine -- so by construction it passes
    # on an untouched tree. This is the photo cell's exact shape.
    VERIFY_DISCRIMINATES=no
  fi
  echo "[$TAG] verify_discriminates=$VERIFY_DISCRIMINATES for $TASK_NAME (pristine-tree probe)" >> "$DRIVER_LOG"
  if [ "$VERIFY_DISCRIMINATES" = "no" ] && [ "${SUCCESS_METRIC:-verify_passed}" = "verify_passed" ]; then
    echo "[$TAG] ABORT: $TASK_NAME verify PASSES on the pristine tree AND declares verify_passed as its success metric. A model that changes nothing would score a pass. Declare SUCCESS_METRIC (a separate scorer) or make the verify discriminate." >> "$DRIVER_LOG"
    _abort_row "ABORT_VACUOUS_PASS"; return
  fi
  git -C "$WT_DIR" reset --hard "$BASE_SHA" >/dev/null 2>&1
  git -C "$WT_DIR" clean -fd >/dev/null 2>&1

  # Assemble the prompt for this arm.
  local FULL_TASK="$TASK_TEXT"
  case "$ARM" in
    repomap)
      # Salience arm applies to the exploration task -- the whole hypothesis is
      # about finding one file among ~40 in a large tree.
      local MAP; MAP=$(build_repo_map "$WT_DIR")
      FULL_TASK="${MAP}"$'\n\n'"${TASK_TEXT}"
      ;;
    apisurface)
      local SURF; SURF=$(build_api_surface)
      if [ "$SURF" = "__API_SURFACE_UNAVAILABLE__" ]; then
        echo "[$TAG] ABORT: apisurface arm requested but no CryptoKit .swiftinterface found" >> "$DRIVER_LOG"
        _abort_row "ABORT_NO_API_SURFACE"; return
      fi
      FULL_TASK="${SURF}"$'\n\n'"${TASK_TEXT}"
      ;;
  esac
  [ "$USE_NUDGE" = "yes" ] && FULL_TASK="${FULL_TASK}${NUDGE}"

  # ---- B3: control host state immediately before dispatch ----
  local HOST_READY="yes"
  restart_inference_server "$HOST" "$MODEL" "$CTX" || HOST_READY="no"

  local TEL_BEFORE SWAP_B AVAIL_B LOAD_B
  TEL_BEFORE=$(python3 "$HOSTTEL"); IFS='|' read -r SWAP_B AVAIL_B _ LOAD_B <<< "$TEL_BEFORE"

  local START_TS; START_TS=$(date +%s)
  echo "[$TAG] START: $MODEL / $TASK_NAME arm=$ARM ctx=$CTX/$NATIVE timeout=${TMO}s host_ready=$HOST_READY swap=${SWAP_B}MB avail=${AVAIL_B}MB @ $(date '+%Y-%m-%d %H:%M:%S')" >> "$DRIVER_LOG"

  python3 "$WORKER" \
    --model "$MODEL" --host "$HOST" --cwd "$WT_DIR" \
    --task "$FULL_TASK" --verify "$RUN_VERIFY" \
    --max-iters 30 --num-ctx "$CTX" \
    "${ARGS[@]}" \
    > "$LOG" 2>&1 &
  local WPID=$!
  ( sleep "$TMO" && kill -TERM "$WPID" 2>/dev/null ) & local WATCH=$!
  wait "$WPID" 2>/dev/null; local EXIT=$?
  kill "$WATCH" 2>/dev/null; wait "$WATCH" 2>/dev/null

  local TEL_AFTER SWAP_A AVAIL_A
  TEL_AFTER=$(python3 "$HOSTTEL"); IFS='|' read -r SWAP_A AVAIL_A _ _ <<< "$TEL_AFTER"

  local DUR TIMED_OUT FILES ITERS VPASS SELFTEST WARMUP LOOP
  DUR=$(( $(date +%s) - START_TS ))
  TIMED_OUT="false"; [ "$DUR" -ge "$TMO" ] && TIMED_OUT="true"
  # Anchored to the worker's iteration banner. A bare 'iteration' grep also
  # counts end-of-run summary lines and any model prose containing the word,
  # which inflates the count DIFFERENTIALLY for chatty models -- turning
  # s/iteration, the number the whole host-degradation argument rests on, into
  # a function of how talkative a model is.
  ITERS=$(grep -ac -- '--- iteration ' "$LOG" 2>/dev/null); ITERS=${ITERS:-0}

  # warmup_s / loop_s split. The worker logs `warmup_s=N` once it is warm; if
  # that line is absent the run died IN warmup, so all elapsed time was warmup
  # and none of it was work. Never report the two as one number -- v7 row 31
  # spent ~2710s failing to load and it read as 2710s of running.
  WARMUP=$(grep -a -o 'warmup_s=[0-9.]*' "$LOG" 2>/dev/null | tail -1 | cut -d= -f2)
  if [ -z "$WARMUP" ]; then WARMUP="$DUR"; LOOP=0; else
    WARMUP=${WARMUP%.*}
    LOOP=$(( DUR - WARMUP )); [ "$LOOP" -lt 0 ] && LOOP=0
  fi

  # Token accounting, from the worker's single summary line. tokens/s is the
  # CLEAN degradation signal: s/iteration confounds host state with model
  # verbosity, tokens/s does not. Absent line -> -1, never 0, so "not recorded"
  # can never be mistaken for "produced nothing".
  local TOKLINE PROMPT_TOK OUTPUT_TOK DECODE_S OUT_TPS
  TOKLINE=$(grep -a -o 'tokens prompt=[0-9]* output=[0-9]* prefill_s=[0-9.]* decode_s=[0-9.]* out_tps=[0-9.]*' "$LOG" 2>/dev/null | tail -1)
  if [ -n "$TOKLINE" ]; then
    PROMPT_TOK=$(sed -n 's/.*prompt=\([0-9]*\).*/\1/p' <<< "$TOKLINE")
    OUTPUT_TOK=$(sed -n 's/.*output=\([0-9]*\).*/\1/p' <<< "$TOKLINE")
    DECODE_S=$(sed -n 's/.*decode_s=\([0-9.]*\).*/\1/p' <<< "$TOKLINE")
    OUT_TPS=$(sed -n 's/.*out_tps=\([0-9.]*\).*/\1/p' <<< "$TOKLINE")
  else
    PROMPT_TOK=-1; OUTPUT_TOK=-1; DECODE_S=-1; OUT_TPS=-1
  fi

  VPASS="no"; grep -aq "VERIFY PASSED" "$LOG" 2>/dev/null && VPASS="yes"

  # Did the self-test actually run, and did it pass? Distinct from the build.
  # "built but never wired the test up" and "wired it and the crypto fails" are
  # different results and v7 collapsed both into verify_passed=no.
  # FABLE BLOCKERS 1 AND 2 (2026-08-23). The first cut of this greped the WHOLE
  # log and tested `missing` BEFORE `pass`, and both halves were wrong:
  #
  # 1. FALSE `missing` ON RUNS THAT PASSED. The worker logs a 300-char preview of
  #    every tool result, and the pristine binary's unknown-command handler
  #    prints "Unknown command: ..." plus a Usage line containing
  #    "stream-selftest" well inside that window. A model that runs
  #    `swift run Clamshell` with no args while exploring -- or probes
  #    confirmation-bridge-selftest against a stale binary before its own build
  #    lands, both of which the task text actively encourages -- injects a match
  #    mid-run. Model prose could too. That match then OVERRODE a genuine pass.
  #
  # 2. `fail` ABSORBED "VERIFY NEVER RAN". The worker only runs verify after the
  #    loop, so a run killed at the wall, stopped at a ceiling, or load_failed
  #    never reaches it -- and was still recorded as `fail`, which this column
  #    defines as "wired the self-test up and the crypto is wrong". That is v7's
  #    `context_ceiling` disease reproduced inside the column added to cure it.
  #
  # Both fixes are the same idea: judge only the VERIFY SECTION of the log, and
  # let a real pass win over any textual heuristic.
  SELFTEST="na"
  if [ "$TASK_NAME" = "clamshell-confirmation-bridge" ]; then
    local VERIFY_LOG
    VERIFY_LOG=$(sed -n '/\[worker\] running verify command:/,$p' "$LOG" 2>/dev/null)
    if [ -z "$VERIFY_LOG" ]; then
      SELFTEST="not_run"          # verify never reached -- NOT a crypto result
    elif printf '%s' "$VERIFY_LOG" | grep -aq "VERIFY TIMED OUT"; then
      SELFTEST="not_run"          # verify overran its own 300s cap; says nothing
                                  # about the crypto. Added with the worker's
                                  # TimeoutExpired catch -- without this branch
                                  # the new catch would land in `fail` below and
                                  # reintroduce exactly the conflation blocker 2
                                  # was raised to remove.
    elif [ "$VPASS" = "yes" ]; then
      SELFTEST="pass"             # a real pass outranks every heuristic below
    elif printf '%s' "$VERIFY_LOG" | grep -aqiE "unknown (command|subcommand)|no such subcommand"; then
      SELFTEST="missing"          # built, but never wired the subcommand up
    else
      SELFTEST="fail"             # wired up and genuinely failed
    fi
  fi

  # Baseline-relative, so staged or committed work counts like unstaged work.
  local TRACKED NEWF
  NEWF=$(git -C "$WT_DIR" status --porcelain -uall 2>/dev/null | grep -c '^??' | tr -d ' ')
  TRACKED=$(git -C "$WT_DIR" diff --name-only "$BASE_SHA" 2>/dev/null | wc -l | tr -d ' ')
  FILES=$((TRACKED + NEWF))

  # Archive the diff per repeat AND per arm before the next run resets this
  # worktree. v7c lost one run's evidence to a shared filename; the CSV says
  # whether something changed, only the diff says whether it was any good.
  git -C "$WT_DIR" diff "$BASE_SHA" > "$STEM.diff" 2>/dev/null
  git -C "$WT_DIR" status --porcelain -uall >> "$STEM.diff" 2>/dev/null

  # UNTRACKED CONTENT, not just filenames. `git diff` shows tracked changes only,
  # and the status line above records that a file was CREATED but never what was
  # in it -- then the next run's `reset --hard && clean -fd` destroys it. On a
  # greenfield cell the new files ARE the deliverable, so the archive was
  # discarding the primary evidence and keeping the footnote.
  #
  # This was not theoretical. qwen3-coder:30b's photo r1 archived as a 3-line
  # diff -- an import line and a JSX tag -- which reads as a trivial edit. The
  # real work was `components/PhotoUpload.tsx`, created and never captured, and
  # the run still scored a PASS. Every greenfield verdict was resting on a file
  # count plus `npm run build`, with the artifact that would show whether the
  # work was any good already deleted.
  # Written to a SEPARATE file, never appended to "$STEM.diff". Testing this
  # appended-in-place cost a 35GB runaway: the artifact was inside the worktree,
  # so `ls-files --others` listed the artifact itself and cat'd it into itself.
  # In production STEM lives in OUTDIR, outside WT_DIR, so that exact loop cannot
  # occur -- but a rule that depends on two paths never coinciding is not a rule.
  # Separate file, an explicit self-skip, and hard caps on count and total bytes.
  local NEWSTEM="$STEM.newfiles"
  {
    echo "=== UNTRACKED FILE CONTENTS (created by the model) ==="
    local COUNT=0 TOTAL=0
    git -C "$WT_DIR" ls-files --others --exclude-standard 2>/dev/null | while read -r NF; do
      case "$NF" in
        node_modules/*|.next/*|dist/*|build/*|.build/*) continue ;;
        *.diff|*.newfiles) continue ;;
      esac
      COUNT=$((COUNT + 1))
      if [ "$COUNT" -gt 60 ]; then
        echo "[truncated: more than 60 new files]"; break
      fi
      SZ=$(wc -c < "$WT_DIR/$NF" 2>/dev/null | tr -d ' ')
      SZ=${SZ:-0}
      TOTAL=$((TOTAL + SZ))
      echo "--- new file: $NF ($SZ bytes) ---"
      if [ "$SZ" -gt 200000 ]; then
        echo "[omitted: $SZ bytes exceeds the 200KB per-file cap]"
      elif [ "$TOTAL" -gt 5000000 ]; then
        echo "[omitted: 5MB total archive cap reached]"; break
      elif LC_ALL=C grep -qI . "$WT_DIR/$NF" 2>/dev/null; then
        cat "$WT_DIR/$NF" 2>/dev/null
      else
        echo "[omitted: binary]"
      fi
      echo ""
    done
  } > "$NEWSTEM" 2>/dev/null

  local TRANSCRIPT; TRANSCRIPT=$(grep -o "/Users/user/bin/ollama-worker-logs/[0-9TZ]*\.json" "$LOG" | tail -1)

  # stop_reason, most specific first. The config/native split is the whole
  # point: config_ceiling is a knob we can turn next round, native_ceiling is
  # the model's own limit and there is nothing to raise.
  local STOPR="none"
  if [ "$EXIT" -eq 3 ] || grep -aq "LOAD FAILED" "$LOG" 2>/dev/null; then
    STOPR="load_failed"
  elif grep -aq "CONTEXT CEILING" "$LOG" 2>/dev/null; then
    if [ "$CTX" -ge "$NATIVE" ]; then STOPR="native_ceiling"; else STOPR="config_ceiling"; fi
  elif [ "$TIMED_OUT" = "true" ]; then
    STOPR="timeout"
  elif [ "$EXIT" -eq 0 ]; then
    STOPR="converged"
  elif [ "$EXIT" -eq 2 ]; then
    STOPR="iter_cap"
  fi

  echo "[$TAG] DONE: $MODEL / $TASK_NAME arm=$ARM exit=$EXIT dur=${DUR}s warmup=${WARMUP}s loop=${LOOP}s timedout=$TIMED_OUT files=$FILES iters=$ITERS stop=$STOPR selftest=$SELFTEST swap ${SWAP_B}->${SWAP_A}MB avail ${AVAIL_B}->${AVAIL_A}MB" >> "$DRIVER_LOG"

  echo "$MODEL,$BACKEND,$TASK_NAME,$ARM,$REP,$EXIT,$VPASS,$SELFTEST,$DUR,$WARMUP,$LOOP,$TIMED_OUT,$FILES,$ITERS,$CTX,$NATIVE,$STOPR,$HOST_READY,$PROMPT_TOK,$OUTPUT_TOK,$DECODE_S,$OUT_TPS,$SWAP_B,$SWAP_A,$AVAIL_B,$AVAIL_A,$LOAD_B,${TRANSCRIPT:-none}" >> "$RESULTS_CSV"
}

# Deterministic per-rep shuffle. Seeded by rep so a round is reproducible and a
# reviewer can regenerate the exact order that produced a CSV -- an unseeded
# shuffle would make position unrecoverable after the fact, which is the same
# class of mistake as not recording context per row.
shuffled_roster() {
  local SEED="$1"; shift
  printf '%s\n' "$@" | python3 -c "
import random, sys
rows = [r for r in sys.stdin.read().splitlines() if r.strip()]
random.Random($SEED).shuffle(rows)
print('\n'.join(rows))
"
}

unload_model() {
  curl -s --max-time 30 "$2/api/generate" -d "{\"model\":\"$1\",\"keep_alive\":0}" >/dev/null 2>&1
  echo "[unload] $1 on $2" >> "$DRIVER_LOG"
}
