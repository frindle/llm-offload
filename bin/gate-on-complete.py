#!/usr/bin/env python3
"""Auto-gate hook: run the Studio gate on a completed dispatch. ADVISORY ONLY.

THE MODEL CALL GOES THROUGH THE QUEUE, NEVER DIRECTLY. the operator's constraint, and it
restores my own original design -- the first version called ollama at
127.0.0.1:11434 via subprocess, which would have run CONCURRENTLY with real
dispatches: contending for the GPU, bypassing the VRAM guard, and forcing model
swaps. Everything this project learned about serialising GPU work, discarded in
the wiring. The queue exists precisely to schedule this.

So the gate splits by what needs a GPU:

  DECIDABLE (scope, completeness, verify-quality) -> pure Python, run HERE, now.
  MODEL REVIEW                                    -> ENQUEUED as a --runner job.

Until the review job lands, .gate.json carries the decidable findings plus
review="pending". It must never look like a clean pass while the review is still
queued -- same rule as a visible skip.

Designed so the queue's integration is ONE CALL, not a feature:

    subprocess.Popen(["python3", str(Path.home()/"bin"/"gate-on-complete.py"),
                      "--job-id", job["id"], "--cwd", job["cwd"],
                      "--task-file", job["task_file"], "--verify", job.get("verify") or ""])

Everything else -- diff acquisition, skip handling, output placement -- lives here,
so ollama-queue.py owns none of it and can be reverted by deleting one call.

TWO DESIGN RULES, both learned the hard way today:

1. A SKIPPED GATE MUST BE VISIBLY SKIPPED. If a diff cannot be obtained, this
   still writes a .gate.json with verdict="skipped" and the reason. Absence of a
   verdict must never be readable as a pass -- that is how "no findings" gets
   confused with "not checked", which is the single most dangerous thing a gate
   can do.

2. NEVER BLOCKS, NEVER FAILS THE JOB. Exit code is always 0. A gate that can
   break a dispatch is worse than no gate: the dispatch is the real work, this is
   commentary on it.
"""
import os as _qc_os, sys as _qc_sys
_qc_dir = _qc_os.path.dirname(_qc_os.path.realpath(__file__))
if _qc_dir not in _qc_sys.path:
    _qc_sys.path.append(_qc_dir)
import queue_config
import argparse, importlib.util, json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path


# GIT_OPTIONAL_LOCKS=0 (2026-10-02, Rivian s5 566e7cc77629 / 5059223eacc3 / 454979f2a248):
# a plain `git status` REWRITES .git/index (it takes index.lock to refresh stat
# info). Our observers (worktree snapshots, dirty checks) run it with timeouts
# against .git dirs inside the iCloud Desktop repos, where it can be slow -- and a
# held or timeout-orphaned index.lock made auto-harness-check's `git checkout`
# fail with "index.lock: File exists" -> "HARNESS ERROR: git is unusable". With
# this set, read-only commands never take the optional lock; writes still lock.
os.environ.setdefault("GIT_OPTIONAL_LOCKS", "0")

# HERE is where THIS file and its sibling checkers live (gate.py, signoff.py,
# verify-relevance.py). BIN is where the QUEUE's state lives (state file, logs,
# ollama-queue.py, handoff-emit.py). In production both are ~/bin; in a worktree
# canary they differ, and the checkers under test must be the worktree's.
HERE = Path(__file__).resolve().parent
BIN = Path(os.environ.get("GATE_BIN") or (Path.home() / "bin")).expanduser()
# queue state (state JSON, queue logs) lives in the state dir; GATE_BIN still wins, as it did when state sat next to the scripts
STATE_DIR = Path(os.environ.get("GATE_BIN") or queue_config.state_dir()).expanduser()
GATE = HERE / "gate.py"
SIGNOFF = HERE / "signoff.py"
RELEVANCE = HERE / "verify-relevance.py"
# GATE_TEST_MODE=1: no review enqueue, no notify, no handoff render. Reads of
# the queue state still happen (fail-open). Lets a canary drive the FULL
# decidable path (gate.py -> relevance -> signoff --evaluate -> --auto) against
# a fixture repo without touching the live queue.
TEST_MODE = os.environ.get("GATE_TEST_MODE") == "1"

# COMPLETED-CODE-DROP. When a dispatch reaches a TERMINAL gate verdict, drop its
# finished deliverable (the .diff + the changed files' post-change contents + a
# SUMMARY.md) into a browsable, verdict-keyed tree so the operator can eyeball what the
# model produced without opening the queue logs or the worktree. Pass/ vs Failed/
# keyed off the verdict. This lives UNDER the iCloud Desktop tree by request, so
# drops are kept to the diff + changed files (never a whole worktree). Overridable
# for the self-test (module global, reassigned + restored there).
COMPLETED_ROOT = Path(os.environ.get("GATE_COMPLETED_ROOT")
                      or (Path.home() / "Desktop" / "GitHub Projects"
                          / "Ollama" / "Completed")).expanduser()
# Skip copying any single changed file larger than this into the drop (the diff
# still carries the full change); keeps iCloud-Desktop drops small.
COMPLETED_MAX_FILE_BYTES = int(os.environ.get("GATE_COMPLETED_MAX_FILE_BYTES") or 1_000_000)

# --- Two-tier gate routing (2026-09-04, the operator's design; wired after the 14b bake-off) ---
# The cheap PRE-gate runs on Unraid (qwen3:14b, ~10.4G VRAM @ num-ctx 6144 -- fits
# the 3080 with headroom, zero CPU spillover). Bake-off vs the Studio 27B over 16
# real .gate.json diffs (2026-09-04): 88% agreement, 0 false-FAIL, 2 false-PASS;
# catches 2/4 known-defect diffs. So the pre-gate is a cheap ESCALATION TRIGGER,
# never the authority: a NON-PASS pre-gate verdict escalates an authoritative
# re-gate to the Studio 27B (qwen3.8:27b-q4_K_M), whose verdict SUPERSEDES the
# pre-gate's in .gate.json. A pre-gate PASS is NON-TERMINAL by construction --
# signoff stays shadow-mode (auto-approves nothing) and Claude reads every diff
# at merge -- so a pre-gate false-PASS never causes a bad autonomous merge.
# The re-gate carries a distinct 'regate-' label so it does NOT re-fire the
# 'gate-' loop guard. Everything is env-overridable; to roll back to the
# single-tier 27B gate set GATE_PREGATE_MODEL=qwen3.8:27b-q4_K_M
# GATE_PREGATE_HOST=studio GATE_PREGATE_NUM_CTX=32768 GATE_TWO_TIER=0.
PREGATE_HOST    = os.environ.get("GATE_PREGATE_HOST", "unraid")
PREGATE_MODEL   = os.environ.get("GATE_PREGATE_MODEL", "qwen3:14b")
PREGATE_NUM_CTX = int(os.environ.get("GATE_PREGATE_NUM_CTX", "6144"))
REGATE_HOST     = os.environ.get("GATE_REGATE_HOST", "studio-db")
# (2026-10-01) The regate runs on Darkbloom now (host "studio" IS the Darkbloom lane).
# qwen3.8-27B can't run on this M4 Max, so the default is Qwen3.6-35B-A3B. NOT yet
# bake-off-measured against the qwen3.8 baseline -- see the migration plan, section E.
REGATE_MODEL    = os.environ.get("GATE_REGATE_MODEL", "qwen3.6-35b-a3b-vl-mtp-mxfp8")
REGATE_NUM_CTX  = int(os.environ.get("GATE_REGATE_NUM_CTX", "32768"))
TWO_TIER        = os.environ.get("GATE_TWO_TIER", "1") != "0"

# --- SECOND OPINION: cross-family independent review on Unraid (2026-10-01) ------
# The operator: "a PASS must not be decided by one model family." Every tier that can
# produce a PASS today is Qwen -- the Unraid pre-gate is qwen3:14b and the
# authoritative Darkbloom re-gate is qwen3.6-35b-a3b. A shared-family blind spot
# (same tokenizer, same instruction-tuning lineage, same failure modes) is
# therefore invisible: both can miss the same defect for the same reason.
#
# So: when a gate reaches verdict=pass on a CODE diff, enqueue an INDEPENDENT
# review on Unraid with a model from a DIFFERENT family, label 'secondop-<id>'.
#
# SEMANTICS (deliberately conservative -- decided here, not left implicit):
#   * ADDITIVE ONLY. The second opinion NEVER changes payload["verdict"],
#     payload["issues"], payload["counts"], gate_authority, signoff, auto-fix or
#     the Pass/Failed drop bucket. Its rows land in their own namespace
#     (second_opinion_issues) so no verdict recompute can ever see them.
#   * DISAGREEMENT SURFACES AS A FLAG, never a flip. A non-PASS second opinion
#     sets second_opinion_agreement="disagree", second_opinion_disagreement=True
#     and appends a human-readable line to payload["second_opinion_flags"] --
#     which is what the coordinator reads at the merge diff-glance.
#   * It is advisory and best-effort: a failed/missing report records
#     second_opinion.review="failed" and changes nothing else. A gate PASS is
#     never held waiting on it (it is non-terminal by construction, exactly like
#     the pre-gate PASS it reviews).
#   * It fires ONCE per parent (guarded on second_opinion_label), at whichever
#     tier produced the PASS -- including an authoritative re-gate PASS that
#     OVERTURNED a pre-gate non-pass, since that PASS is Qwen-only too.
#
# Model: gemma4:12b-it-q4_K_M -- family "gemma4", disjoint from every qwen tier,
# 7.56GB of weights against Unraid's 10.3GB usable-VRAM gate, run at a
# deliberately small num_ctx so the KV cache cannot close that ~2.7GB margin. It
# has NO UNRAID_CONFIRMED_SAFE_CTX entry (that table means MEASURED, and this is
# not measured yet) -- ollama-worker's live post-warmup spillover abort stays the
# backstop, and a measurement request is filed for the operator.
SECOND_OPINION        = os.environ.get("GATE_SECOND_OPINION", "1") != "0"
SECOND_OPINION_HOST   = os.environ.get("GATE_SECOND_OPINION_HOST", "unraid")
SECOND_OPINION_MODEL  = os.environ.get("GATE_SECOND_OPINION_MODEL",
                                       "gemma4:12b-it-q4_K_M")
# 4096, not the pre-gate's 6144: a 12B's weights are smaller than qwen3:14b's but
# unmeasured on this box, so buy margin with context rather than assume it.
SECOND_OPINION_NUM_CTX = int(os.environ.get("GATE_SECOND_OPINION_NUM_CTX", "4096"))
# Minimum changed code lines to bother. 0 = every code diff (the default: the operator's
# ask is "every code diff the pre-gate passed", and a 3-line diff is cheap).
SECOND_OPINION_MIN_LINES = int(os.environ.get("GATE_SECOND_OPINION_MIN_LINES", "0"))

# --- AUTO-FIX: bounded auto-requeue of a gate-FAILing dispatch (2026-09-11) ----
# When a completed CODING dispatch reaches a TERMINAL gate verdict that a re-run
# of the model could plausibly fix, requeue it -- same sealed verify, same
# worktree baseline, the gate's concerns fed back in as guidance -- so the human
# coordinator is not dragged into routine gate-fix loops. Only DECIDABLE,
# model-actionable failures requeue; anything ambiguous/undecidable ESCALATES.
#
# MODE, deliberately mirroring signoff.py's shadow rollout (feedback:
# suspect-the-grader / high-bar-for-model-is-the-problem). "shadow" (DEFAULT)
# computes and RECORDS the decision + the exact requeue command into .gate.json
# and AUTO-FIX-QUEUE.md, and enqueues NOTHING. "live" actually requeues. It ships
# shadow so the operator can read, over real jobs, whether the classifier fires correctly
# and the requeue commands are right, at zero risk of a loop or a tampered verify.
# Flip to live ONLY after the shadow log shows it agreeing with hand judgement.
AUTOFIX_MODE       = os.environ.get("GATE_AUTOFIX_MODE", "shadow").lower()
# Loop guard: a dispatch may be auto-requeued at most this many times before the
# harness gives up and escalates to a human. round 0 = original human dispatch,
# so MAX_ROUNDS=2 means at most attempts at round 1 and round 2.
AUTOFIX_MAX_ROUNDS = int(os.environ.get("GATE_AUTOFIX_MAX_ROUNDS", "2"))
# Trust the (authoritative 27B) MODEL REVIEWER's code-high as a requeue trigger?
# Default OFF: a reviewer code-high with no red verify is exactly the false-FAIL
# class the two-tier gate exists to contain, and a false-FAIL that auto-requeues
# burns GPU chasing a defect that is not there. Off => such a verdict ESCALATES.
AUTOFIX_TRUST_REVIEWER = os.environ.get("GATE_AUTOFIX_TRUST_REVIEWER", "0") == "1"

# --- ESCALATION TRIAGE (Task E; operator 2026-09-18) ------------------------------
# The drain side of the line above. AUTOFIX_TRUST_REVIEWER off is correct -- a
# reviewer code-high with a green verify must NOT auto-requeue a second model onto
# already-correct code -- but the consequence is that every such job PARKS in
# needs_opus and sits there until the operator notices and asks a Claude session to look.
# Measured base rate (whole daemon log, 222 gate decisions to 2026-09-18): FIVE
# reviewer-code-high escalations, and all five are the SAME pathology -- the
# reviewer diffed old-vs-new and flagged the INTENDED REMOVAL of the old behaviour
# the task itself required removing (twice the finding text even says the
# replacement is "safer and more accurate"). Zero were a real regression.
#
# So this triage answers exactly one question, DETERMINISTICALLY, with NO model
# call (cost/latency ~0, nothing to hallucinate): is every flagged literal a line
# this diff REMOVED and never re-added, AND does the job's own TASK.md quote that
# literal as the behaviour being changed? Only then is the finding provably the
# old contract, and only then may the park be cleared. Anything else -- a flagged
# literal in ADDED code, an out-of-scope file, an unproven/vacuous verify, a dirty
# baseline, an unreadable TASK -- stays parked, WITH the analysis attached so the
# eventual human/Opus pass is a read rather than a re-derivation.
#
# MODE, mirroring AUTOFIX_MODE / AUTO_PIPELINE_MODE / RUNSTATUS_JANITOR_MODE:
# "shadow" (DEFAULT) computes the decision, records it on .gate.json and in
# ESCALATION-TRIAGE.md, and resolves NOTHING; "live" runs the resolve. n=5 is far
# too thin a base rate to auto-clear a human worklist item on day one, and resolve
# REMOVES the row from queue state, so this ships shadow and stays shadow until
# the log shows it agreeing with hand judgement. Read once at daemon start --
# flipping to live needs a daemon restart.
ESCALATION_TRIAGE_MODE = os.environ.get("GATE_ESCALATION_TRIAGE", "shadow").lower()
# Minimum contiguous normalised characters of a flagged literal that TASK.md must
# itself quote before the removal counts as contract-mandated. 40 is long enough
# that an incidental overlap (a keyword, a short call) cannot clear the bar.
ESCALATION_TRIAGE_MIN_CITE = int(os.environ.get("GATE_ESCALATION_TRIAGE_MIN_CITE", "40"))
# STAGE 4 (CONTRACT SUPERSESSION), added 2026-09-18 after three parks of a SECOND
# reviewer false-positive shape that stage 3 structurally cannot see:
#
#   TASK.md's "Required change"/"Exact contract" mandates a WHOLE-SIGNATURE rewrite
#   of an existing function -- old parameter(s) gone, old return shape gone, a NEW
#   signature specified byte-for-byte. The model does exactly that. The reviewer
#   (usually the DETERMINISTIC harness guards `harness-dropped-member` /
#   `harness-guard-removed`, which compare literal sets before vs after and cannot
#   read the task) then flags the old default, the old `--flag`, or an old branch as
#   "silently dropped" -- a REAL_DEFECT verdict for code the contract ordered deleted.
#
# Stage 3 cannot clear these because it demands TASK.md quote the REMOVED literal;
# here TASK.md quotes the REPLACEMENT instead (the Must-contain list matches the NEW
# code). So stage 4 asks the mirror-image question: did this diff replace
# `def NAME(<old params>)` with a `def NAME(<new params>)` that TASK.md dictates as a
# WHOLE, EXACT line -- and is the flagged line either that old signature or a line in
# the body that rewrite replaced? That is checkable with no model call.
#
# It is deliberately SHADOW BY CONSTRUCTION, not by env var: a stage-4 match returns
# action "park" on every path (see classify_escalation_triage). An exact whole-line
# TASK citation of the NEW signature is strong, but it is NOT proof that every line
# the rewrite dropped was meant to go -- a model can honour the new signature and
# still lose an unrelated guard inside the same body. That residual risk is precisely
# what GATE_AUTOFIX_TRUST_REVIEWER=off exists to refuse, so stage 4 buys a human a
# fast READ ("this finding is the old contract, here is the superseding line"), never
# a clearance. Flipping it to auto-resolve is a deliberate code change, not a flag.
#
# Minimum length of the rewritten `def` line before an exact TASK.md quotation counts.
# Unlike MIN_CITE this is an EXACT WHOLE-LINE containment test, so a short bar is safe
# (no partial overlap can ever qualify); 16 chars merely excludes toys like `def f():`.
ESCALATION_TRIAGE_MIN_SIG = int(os.environ.get("GATE_ESCALATION_TRIAGE_MIN_SIG", "16"))

# --- END-TO-END AUTO-PIPELINE (Task C; operator 2026-09-17) -----------------------
# The SUCCESS path, the mirror of the auto-fix ladder's failure path. the operator's
# intent: "unless something fails the process should be automated all the way from
# the auto author to the code fix." Two transitions the harness otherwise stops at
# for a human:
#   STAGE 3 (advance): an auto-author/refine job whose harness reached a CLEAN gate
#     AND a PROVEN relevance verdict ('relevant', 0 survivors) -> confirm the draft
#     (seal the fixture) and enqueue the real CODING job. This is the step
#     ollama-dispatch-auto's pause_for_review prints for a human today.
#   STAGE 6 (apply): a CODING job whose sealed gate/regate is a CLEAN PASS -> land
#     the fix per the per-repo apply policy below.
#
# SEPARATE, OFF-by-default switch from GATE_AUTOFIX_MODE so the failure ladder and
# the success pipeline enable INDEPENDENTLY. "shadow" (DEFAULT): decide + record
# into .gate.json, act on nothing. "live": perform the advance/apply, each exactly
# ONCE (idempotency-guarded), halting + surfacing on any failure. Flip to live only
# after the shadow record shows the decisions are right -- and the queue daemon
# must be restarted to pick up the new code (see the restart checklist).
#
# HARD GUARDRAIL (absolute, shared with Task D): this pipeline NEVER edits or
# weakens verify.sh / the gate / check_literals.py / fixtures. STAGE 3 only seals
# the model-authored fixture (draft --confirm) and enqueues a coding job against
# the UNCHANGED sealed verify; STAGE 4 apply only copies TARGET code to main and
# NEVER merges the worktree branch. The gate itself enforces the no-tamper rule
# for the coding job (gate.py --harness-authoring is set ONLY for authoring jobs).
AUTO_PIPELINE_MODE = os.environ.get("GATE_AUTO_PIPELINE", "shadow").lower()
# Repos that redeploy from main after CI (feedback_resell_auto_deploy_after_ci):
# a clean-passing fix is copied to main automatically. Everything else stops at
# "PASS -- ready to apply" for a human to land. Keyed by the repo/worktree
# basename (an auto-* worktree is named wt[-slice]-<label>, so the ORIGIN repo is
# resolved via git, not the worktree name). Env GATE_AUTO_DEPLOY_REPOS (comma-sep)
# extends this set without a code change.
AUTO_DEPLOY_REPOS = set(
    r.strip() for r in os.environ.get("GATE_AUTO_DEPLOY_REPOS", "").split(",") if r.strip()
) | {"resell-tracker"}

# RUN-STATUS JANITOR (2026-09-17, the operator: "why are we not automating these being
# handled so I don't have to keep asking?"). The Run Status dashboard fills with
# finished jobs a human then clears one-by-one, but most never needed eyes: eval/
# bake-off/probe arms, dead/superseded slice fragments, and the intermediate
# authoring/refine STAGE rows a single feature leaves behind (auto-author-<x> +
# auto-refine-<x>-rN + a final row). This janitor auto-archives ONLY those safe
# categories at completion, via the SAME recoverable mechanism the dashboard
# "clear" button uses (POST /api/runs/<id>/clear on the queue-api), and SURFACES
# everything else. FAIL-SAFE: uncertain -> surface, never archive. Switch-gated
# 'shadow' (log-only) by default; read ONCE at daemon start, so flipping to 'live'
# needs a daemon restart. NEVER passes the sign-off override -- the endpoint's own
# refusal of awaiting-sign-off jobs is honoured as defense-in-depth.
RUNSTATUS_JANITOR_MODE = os.environ.get("GATE_RUNSTATUS_JANITOR", "shadow").lower()
import os as _qc_os, sys as _qc_sys
_qc_dir = _qc_os.path.dirname(_qc_os.path.realpath(__file__))
if _qc_dir not in _qc_sys.path:
    _qc_sys.path.append(_qc_dir)
import queue_config
QUEUE_API_URL = (os.environ.get("GATE_QUEUE_API") or queue_config.queue_api()[0]).rstrip("/")
_GH_PROJECTS_ROOT = str((Path.home() / "Desktop" / "GitHub Projects"))
# 2026-11-09: the real folder became "GitHub Projects.nosync" ("GitHub Projects" is a symlink to it), and
# job cwds are stored resolved, so both spellings name the projects tree.
_GH_PROJECTS_ROOTS = (_GH_PROJECTS_ROOT, _GH_PROJECTS_ROOT + ".nosync")
_SLICE_RUNS_DIR = Path(os.environ.get(
    "OLLAMA_DISPATCH_HOME", str(Path.home() / ".ollama-dispatch"))) / "slice-runs"
# job statuses that are TERMINAL enough to consider archiving (anything else is
# still live/active and always surfaces)
_JANITOR_TERMINAL = {"done", "failed"}
# a slice-fragment dispatch label is "<feature>-s<N>-<slug>" (ollama-dispatch-slice
# enqueues each slice as f"{plan_label}-{slice_id}"; slice ids are "s<N>-...")
_SLICE_FRAG_RE = re.compile(r"^(?P<feat>.+)-s\d+-\S")
# eval / bake-off / probe MEASUREMENT arms (in addition to handoff-emit's own
# EVAL_LABEL_PREFIXES, reused as the single source of truth): explicit probe/eval
# label shapes the operator named (clamp-*, *-trap-*, *-adequate, capability-*, *probe*).
_EVAL_LABEL_RE = re.compile(r"(^clamp-)|(-trap-)|(-adequate$)|(probe)|(^capability-)", re.I)


_HANDOFF_MOD = None      # cache: None=unloaded, False=load failed, module=loaded


def _handoff_mod():
    """Lazy-load ~/bin/handoff-emit.py for its ONE sign-off/eval predicates
    (signoff_blocks_acting, _label_is_eval) so this janitor and the dashboard can
    never disagree about which jobs a human still owes a decision. Cached; fail-open
    (None) so an import problem degrades to fail-safe (surface), never a crash."""
    global _HANDOFF_MOD
    if _HANDOFF_MOD is not None:
        return _HANDOFF_MOD or None
    _HANDOFF_MOD = False
    try:
        # load from THIS script's dir (handoff-emit.py sits beside it), not the
        # test-mutable BIN, so the eval/sign-off predicates are always available.
        spec = importlib.util.spec_from_file_location(
            "_gc_handoff_emit", Path(__file__).resolve().parent / "handoff-emit.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _HANDOFF_MOD = m
    except Exception:
        _HANDOFF_MOD = False
    return _HANDOFF_MOD or None


def _load_scaffold_basenames() -> set:
    """SINGLE source of truth for dispatch-scaffold basenames:
    ollama-queue.py::_SCAFFOLD_BASENAMES. The apply step excludes these so it only
    copies TARGET code to main, never the verify/fixture/check (the guardrail).
    Fail-OPEN to a small built-in set if the queue module cannot load -- so a
    failed import can never cause a harness file to be treated as a target."""
    try:
        spec = importlib.util.spec_from_file_location(
            "_olq_scaffold_gc", BIN / "ollama-queue.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        s = getattr(m, "_SCAFFOLD_BASENAMES", None)
        if s:
            return set(s)
    except Exception:
        pass
    return {"verify.sh", "verify.test.ts", "verify.test.js", "check_literals.py",
            "test_fixture.py", "refimpl.py", "TASK.md", "AUTO-TASK.md",
            "auto-harness-check.py", ".dispatch-harness.json", ".preflight-state.json"}


_SCAFFOLD_BASENAMES = _load_scaffold_basenames()

# Files the PIPELINE itself drops into a harness-authoring worktree (the vetted, read-only
# environment fixtures the scaffold installs and auto-harness-check restores byte-for-byte).
# They are not in ollama-queue.py::_SCAFFOLD_BASENAMES, so on an auto-author job (no pinned
# digests yet) the 963-line dispatch-env.ts rode into the diff as a "new file" and the
# reviewer cited it against the real target (b90e77ace2b2: "lib/costcoReceiptMatch.ts:976"
# is dispatch-env.ts:949). Unioned here so the gate does not depend on the queue module
# (which the live daemon only re-reads on restart) carrying them.
PIPELINE_HARNESS_BASENAMES = frozenset({"dispatch-env.ts", "dispatch_env.py"})
_SCAFFOLD_BASENAMES = set(_SCAFFOLD_BASENAMES) | PIPELINE_HARNESS_BASENAMES


def _manifest_authored(cwd) -> set:
    """Paths the scaffold's .dispatch-harness.json declares as authored harness output
    (the same exemption the queue's launch-baseline count grants). Empty on any error."""
    try:
        d = json.loads((Path(cwd) / ".dispatch-harness.json").read_text()) or {}
        return {h for h in (d.get("authored") or []) if isinstance(h, str) and h}
    except Exception:
        return set()

# --- MODEL-FALLBACK LADDER (Tier 1; operator 2026-09-17) --------------------------
# When a job's OWN model has exhausted its self-fix budget on a CAPABILITY failure
# (the classifier said `requeue` but the round cap or the anti-thrash guard blocked
# it), we do NOT go straight to a human. First we retry the SAME sealed task+verify
# with the NEXT model down a configurable ladder -- each retry runs the FULL gate
# (verify + relevance), so a different model NEVER bypasses any gate; it only
# "succeeds" by passing. Only when the ladder is EXHAUSTED does the job park as
# needs_opus (Tier 2). This is ADDITIVE to Tier 0 (same-model self-fix / re-slice)
# and applies ONLY to capability failures -- JUDGMENT failures (verdict=error,
# undecidable, dirty baseline, reviewer-only code-high) classify straight to
# `escalate` and never touch the ladder, because a different qwen cannot resolve
# "is this test relevant / is this diff really wrong".
#
# CONFIG (editable WITHOUT a code change): env OLLAMA_DISPATCH_MODEL_LADDER (a
# comma-separated model list) wins; else ~/.ollama-dispatch/model-ladder.json (a
# JSON list, or {"ladder": [...]}); else the seeded default below. Each entry is a
# model string OR {"model":..,"host":..}; host defaults to studio (the dense ladder
# models won't fit the 12GB Unraid card, and all GPU work stays on the queue).
DISPATCH_HOME = Path(os.environ.get("OLLAMA_DISPATCH_HOME",
                                    str(Path.home() / ".ollama-dispatch"))).expanduser()
MODEL_LADDER_FILE = DISPATCH_HOME / "model-ladder.json"
# Seed (cheap -> capable, all fit the Studio's 64GB, host pinned to studio):
DEFAULT_MODEL_LADDER = [
    "qwen3.8:27b-q4_K_M",
    "qwen3.6:35b-a3b-q4_K_M",
    "qwen3:32b-q8_0",
    "llama3.3:70b",
]
# The host every dense ladder model is pinned to (see reference: no-unraid-gpu-load
# -- dense >=12B won't fit the 12GB card, so these must land on the Studio).
MODEL_LADDER_DEFAULT_HOST = "studio"


def _normalize_ladder(raw) -> list:
    """PURE. Coerce a raw ladder (list of str or {model,host} dicts) into a clean
    list of {"model": str, "host": str}. Drops malformed/empty entries; host
    defaults to MODEL_LADDER_DEFAULT_HOST (the dense models must land on the
    Studio). Never raises."""
    out = []
    seen = set()
    for e in (raw or []):
        if isinstance(e, str):
            model, host = e.strip(), MODEL_LADDER_DEFAULT_HOST
        elif isinstance(e, dict):
            model = str(e.get("model") or "").strip()
            host = str(e.get("host") or MODEL_LADDER_DEFAULT_HOST).strip() or MODEL_LADDER_DEFAULT_HOST
        else:
            continue
        if model and model not in seen:
            seen.add(model)
            out.append({"model": model, "host": host})
    return out


def load_model_ladder() -> list:
    """The active fallback ladder, resolved at requeue time so the operator can edit it
    WITHOUT a code change: env OLLAMA_DISPATCH_MODEL_LADDER (comma-separated) wins,
    else MODEL_LADDER_FILE (JSON list or {"ladder":[...]}), else DEFAULT_MODEL_LADDER.
    Fail-open: a missing/garbage file falls through to the default. Returns a list
    of {"model","host"} dicts."""
    env = os.environ.get("OLLAMA_DISPATCH_MODEL_LADDER")
    if env and env.strip():
        nl = _normalize_ladder([s for s in env.split(",")])
        if nl:
            return nl
    try:
        if MODEL_LADDER_FILE.is_file():
            d = json.loads(MODEL_LADDER_FILE.read_text())
            raw = d.get("ladder") if isinstance(d, dict) else d
            nl = _normalize_ladder(raw if isinstance(raw, list) else [])
            if nl:
                return nl
    except Exception:
        pass
    return _normalize_ladder(DEFAULT_MODEL_LADDER)


_SERVED_Q = None


def served_model(model: str, host: str) -> str:
    """The model that will ACTUALLY serve (model, host). On the Darkbloom lane every
    legacy ':'-tagged name is aliased to Darkbloom's default (ollama-queue.py
    _darkbloom_model), so qwen3.8:27b / qwen3.6:35b / qwen3:32b / llama3.3:70b all
    run as the SAME model there. Fail-open to the name itself."""
    global _SERVED_Q
    try:
        if _SERVED_Q is None:
            spec = importlib.util.spec_from_file_location("_olq_served_gc", BIN / "ollama-queue.py")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            _SERVED_Q = m
        q = _SERVED_Q
        if q._darkbloom_url() and (host or "studio") in q.DARKBLOOM_PREFS:
            return q._darkbloom_model(model)
    except Exception:
        pass
    return model


def plan_model_ladder(current_model: str, models_tried, ladder, served=None,
                      current_host: str = "studio") -> dict | None:
    """PURE. The NEXT model+host to try after `current_model` has exhausted its
    self-fix budget, or None when the ladder is exhausted.

      - Start point: the position AFTER current_model in the ladder. The job's
        original model is the tier-1 start, so we only advance DOWNWARD (an earlier,
        cheaper entry was already at least as good as current). If current_model
        isn't in the ladder at all, start at the TOP.
      - Skip any model already in models_tried (and current_model itself) -- the
        loop guard that prevents re-running a model that already failed, and the
        natural bound (the ladder is finite, models_tried only grows).
    Returns {"model","host"} or None."""
    names = [e["model"] for e in (ladder or [])]
    tried = set(models_tried or [])
    if current_model:
        tried.add(current_model)
    # SERVED-model dedupe (2026-10-02): on Darkbloom every ':'-tagged rung aliases
    # to the one default model, so "falling back" to it re-ran the SAME model with
    # the same task -- a no-op rung that burned a full round. `served(model, host)`
    # maps a rung to what will really run; a rung whose served model was already
    # tried is skipped like a tried name.
    sv = served or (lambda m, h: m)
    tried_served = {sv(m, current_host) for m in tried}
    start = (names.index(current_model) + 1) if current_model in names else 0
    for e in (ladder or [])[start:]:
        if e["model"] not in tried and sv(e["model"], e["host"]) not in tried_served:
            return e
    return None


def _signoff_record(job_id: str) -> dict:
    """The signoff.json entry for a job, read THROUGH signoff.py.

    This is the readback that crashed every automated sign-off before
    2026-09-04: emit() called `_load(...)`, a helper that exists only inside
    signoff.py, so the readback raised NameError inside its try/except, the
    error was recorded as `signoff_error: "load: NameError: name '_load' is not
    defined"`, `signoff_required` stayed None, and the `--auto` step (gated on
    signoff_required) never fired -- so no dispatch ever received a verdict
    while every canary stayed green (none of them exercised this path).

    Going through the module rather than json.loads has one more property:
    signoff.py resolves the file via SIGNOFF_DIR, so the gate reads the same
    file signoff.py just wrote, in production and under a canary alike.
    """
    return (_signoff_mod().load_signoffs() or {}).get(job_id) or {}


_SIGNOFF_MOD = None


def _signoff_mod():
    global _SIGNOFF_MOD
    if _SIGNOFF_MOD is None:
        spec = importlib.util.spec_from_file_location("signoff", SIGNOFF)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SIGNOFF_MOD = mod
    return _SIGNOFF_MOD


def scaffold_digests(cwd: Path) -> dict:
    """{relpath: sha256} of the scaffold files the PREFLIGHT pinned for this
    worktree (TASK.md, verify.sh, test_fixture.py, ...), from the ledger
    signoff.py already reads. Empty when there is no ledger entry."""
    try:
        entry = _signoff_mod().ledger_entry_for(str(cwd)) or {}
        d = entry.get("scaffold_sha256") or {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _sha256(p: Path) -> str | None:
    import hashlib
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except Exception:
        return None


def out_dir_for(a) -> Path:
    return (Path(a.out_dir).expanduser() if a.out_dir
            else STATE_DIR / "ollama-queue-logs")


_VERIFY_FILE_RE = re.compile(r"^verify[-_]?(impl)?\.(mjs|cjs|mts|cts|js|ts|py|sh)$")
_VERIFY_TEST_RE = re.compile(r"^verify\.test\.(mjs|cjs|mts|cts|js|ts)$")
_LEGACY_SCAFFOLD_LC = {"task.md", "task.json", "verify.sh", "verify_impl.mjs",
                       "verify_impl.js", "task.txt", "run.json", "refimpl.py",
                       ".preflight-state.json", "auto-task.md"}


def product_files(touched) -> set:
    """PURE. The diff-touched paths that are PRODUCT code -- everything that is
    not dispatch scaffold (TASK.md, verify*, fixture, refimpl, harness state).
    The same name rules get_diff() applies to UNTRACKED files, so a TRACKED
    scaffold edit (a sealed TASK.md the model rewrote) is classed the same way."""
    scaf = {n.lower() for n in _SCAFFOLD_BASENAMES} | _LEGACY_SCAFFOLD_LC
    out = set()
    for p in touched or ():
        name = Path(str(p)).name.lower()
        if name in scaf or _VERIFY_FILE_RE.match(name) or _VERIFY_TEST_RE.match(name):
            continue
        out.add(str(p))
    return out


def no_product_change_finding(touched, job_label, verify_exit) -> dict | None:
    """PURE. The gate PASSED a coding job whose diff changed NO product file
    (2026-09-23, live: the model reverted its own target edit; only the sealed
    TASK.md differed; gate.py saw zero findings against a scaffold-only diff and
    said `pass`; the slicer's empty-deliverable guard was the only thing that
    stopped an untouched baseline landing as DONE). A diff that touches nothing
    but scaffold is not a clean change -- it is NO change, and a green verify over
    it means the verify was green BEFORE the job (already satisfied, or vacuous).
    That is a decidable, high-severity fact, not a reviewer opinion.

    Only for CODING jobs: an auto-author-/auto-refine- job's whole deliverable IS
    the scaffold. Returns the finding to prepend, or None."""
    if _is_authoring_label(job_label):
        return None
    touched = set(touched or ())
    if product_files(touched):
        return None
    shown = ", ".join(sorted(touched)[:6]) or "nothing at all"
    if len(touched) > 6:
        shown += f", +{len(touched) - 6} more"
    green = (" Its verify exited 0 WITHOUT any product change, so the verify was "
             "already green at the launch baseline: the property either already "
             "held or the verify asserts nothing." if verify_exit == 0 else "")
    return {"category": "code", "severity": "high", "source": "no-product-change",
            "file": None, "line": None,
            "what": (f"the diff changes NO product file (touched: {shown}) -- the job "
                     f"delivered nothing; a scaffold-only diff cannot be a pass.{green}")}


def ref_budget_for(num_ctx: int) -> int:
    """PURE. Same formula as code-review-agent.ref_budget_for (which also clamps
    to its ACTUAL --num-ctx): chars of out-of-hunk helper definitions the
    verifier may see at this tier. 0 at <= 8192 (Unraid pre-gate, 6144)."""
    n = int(num_ctx or 0)
    return 0 if n <= 8192 else min(24000, (n - 8192) // 4)


def review_uncertain_claims(report_txt: str) -> list[str]:
    """PURE. Claim headings listed under the review report's
    "## Uncertain" section (code-review-agent build_report). Those rows appear in
    the same summary table as real defects; only the section tells them apart."""
    out, in_unsure = [], False
    for line in report_txt.splitlines():
        if line.startswith("## "):
            in_unsure = line[3:].strip().lower().startswith("uncertain")
            continue
        if in_unsure:
            m = re.match(r"^### \[\w+\] (.+)$", line)
            if m:
                out.append(m.group(1).strip())
    return out


def _is_uncertain_row(table_what: str, uncertain_claims: list[str]) -> bool:
    """The table cell is the claim cut to 100 chars with '|' escaped."""
    cell = table_what.replace("\\|", "|").strip()
    return any(c[:100].strip() == cell or c.startswith(cell)
               for c in uncertain_claims if cell)


def apply_finding(payload: dict, finding: dict) -> None:
    """Prepend a finding to a gate payload and recompute counts + verdict with the
    same mapping gate.py uses (fail if any code-high, else concerns if anything)."""
    issues = [finding] + list(payload.get("issues") or [])
    payload["issues"] = issues
    high = [i for i in issues if i.get("category") == "code" and i.get("severity") == "high"]
    payload["counts"] = {
        "code_high": len(high),
        "code": sum(1 for i in issues if i.get("category") == "code"),
        "input": sum(1 for i in issues if i.get("category") == "input"),
        "total": len(issues)}
    payload["verdict"] = "fail" if high else "concerns"


# The pre-gate reviewer's context window is small (PREGATE_NUM_CTX 6144 on a 14B):
# the intent used to be TASK.md[:1500], which on a scaffolded slice is the header
# + "Confirmed defect" and cuts off BEFORE "Required change", "Must contain" and
# "Scope" -- exactly the sections a reviewer needs to judge scope and completeness
# (#8, 2026-09-23). Keep those sections whole first, then fill with the rest.
REVIEW_INTENT_CAP = 4000
_INTENT_PRIORITY = ("required change", "must contain", "scope", "confirmed defect",
                    "confirmed symptom", "entry point")


def review_intent(task_text: str, cap: int = REVIEW_INTENT_CAP) -> str:
    """PURE. TASK.md trimmed to `cap` chars for the review prompt, keeping the
    priority sections (## Required change / Must contain / Scope / Confirmed
    defect / Entry point) whole and in document order before anything else.
    A text under the cap is returned untouched."""
    text = (task_text or "").strip()
    if len(text) <= cap:
        return text
    parts = re.split(r"(?m)^(?=## )", text)
    head, sections = (parts[0], parts[1:]) if parts and not parts[0].startswith("## ") \
        else ("", parts)

    def _prio(sec):
        title = sec.splitlines()[0][3:].strip().lower() if sec.startswith("## ") else ""
        return any(title.startswith(p) for p in _INTENT_PRIORITY)
    order = sorted(range(len(sections)), key=lambda i: (0 if _prio(sections[i]) else 1, i))
    budget = cap
    keep = {}
    if head:
        h = head.strip()[: max(200, cap // 6)]
        keep[-1] = h + ("\n" if not h.endswith("\n") else "")
        budget -= len(keep[-1])
    for i in order:
        sec = sections[i].rstrip() + "\n"
        if budget <= 0:
            break
        if len(sec) > budget:
            if not _prio(sections[i]) or budget < 120:
                continue
            sec = sec[: budget - 20].rstrip() + "\n[...]\n"
        keep[i] = sec
        budget -= len(sec)
    dropped = len(sections) - sum(1 for k in keep if k >= 0)
    out = "".join(keep[k] for k in sorted(keep))
    if dropped:
        out += f"\n[{dropped} section(s) omitted to fit the review window]\n"
    return out.strip()


def _diff_changed_files(diff_path: Path):
    """Bug #14: the set of file paths a unified diff touches (from its `+++ b/…` /
    `--- a/…` and `diff --git` headers). Empty set if the diff is missing/unreadable
    (caller then skips corroboration -- fail toward keeping findings)."""
    out = set()
    try:
        txt = Path(diff_path).read_text(errors="replace")
    except Exception:
        return out
    for line in txt.splitlines():
        if line.startswith(("+++ ", "--- ")):
            p = line[4:].strip()
            if p and p != "/dev/null":
                p = re.sub(r"^[ab]/", "", p)
                out.add(p)
        elif line.startswith("diff --git "):
            for tok in line.split()[2:]:
                out.add(re.sub(r"^[ab]/", "", tok))
    return out


def _file_in_diff(finding_file, diff_files):
    """True if a reviewer finding's cited file matches any file in the diff, by exact
    path or by basename (the reviewer often cites a bare filename)."""
    ff = str(finding_file or "").strip()
    if not ff:
        return True   # no file cited -> cannot disprove; keep it
    if ff in diff_files:
        return True
    base = ff.rsplit("/", 1)[-1]
    return any(df == ff or df.rsplit("/", 1)[-1] == base for df in diff_files)


def finding_line_out_of_range(cwd, finding, diff_files=()) -> bool:
    """PURE-ish (reads one file). True when a reviewer finding cites a line number BEYOND
    the end of the file it cites -- a position that does not exist, so the claim cannot be
    about that file (b90e77ace2b2: `lib/costcoReceiptMatch.ts:976` on a 21-line file; the
    quoted text was dispatch-env.ts:949, a different file the reviewer was shown). The cited
    file is the worktree copy (exact path, else the diff file with the same basename).
    Abstains (False) on no line, no file, unreadable/missing/binary file, no worktree."""
    try:
        ln = int(finding.get("line") or 0)
        ff = str(finding.get("file") or "").strip()
        if ln <= 0 or not ff or not cwd:
            return False
        root = Path(str(cwd)).expanduser()
        cand = [root / ff]
        base = ff.rsplit("/", 1)[-1]
        cand += [root / df for df in (diff_files or ()) if df.rsplit("/", 1)[-1] == base]
        for c in cand:
            if c.is_file():
                data = c.read_bytes()
                if b"\x00" in data[:8192]:
                    return False
                n = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
                return ln > n
    except Exception:
        pass
    return False


_SO_MOD = None


def _so():
    """slice_obligations.py is retired with the old slicer (use mini-swe). Every caller
    is wrapped in try/except, so plan-context / obligations simply stay absent."""
    raise RuntimeError("slice_obligations retired")


_RESTORED_BY_RE = re.compile(r"\[\s*restored[- ]by\s*:\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*\]", re.I)


# GATE FINDING CHECK (unattended-readiness item 4, 2026-10-04). The authoritative
# re-gate reviewer flags correct code; on a GREEN, relevance-proven verify that parked
# the slice as needs_opus with nothing wrong (a0be91760fb5, a330fff2d326,
# ef2868d21945; c63772e0b710/b1b085738769 "concerns, findings: none listed"). Each
# such finding is now turned into a REPRODUCER by a second model call (queue job
# secondop-<parent>-fcheck, code-review-agent.py --mode refute) and EXECUTED here, in
# a throwaway copy of the exact gated tree, by gate_finding_check.py:
#   reproducer fails on the change (not identically on the baseline) -> CONFIRMED: the
#     finding stands (uncertain cap lifted) and parks exactly as before, reproducer
#     attached;
#   reproducer passes / none produced / pre-existing -> UNCONFIRMED: the row moves to
#     payload['advisory_caveats'] + ADVISORY_LOG and the slice lands;
#   anything else (verifier error, timeout, enqueue/materialize failure, a crash in
#     this code) -> the ORIGINAL verdict stands: today's park. Never silently lands.
# GATE_FINDING_CHECK=off restores the old behaviour exactly.
FINDING_CHECK_MODE = os.environ.get("GATE_FINDING_CHECK", "live").lower()
FC_MODEL = os.environ.get("GATE_FC_MODEL", REGATE_MODEL)
FC_HOST = os.environ.get("GATE_FC_HOST", REGATE_HOST)
FC_NUM_CTX = int(os.environ.get("GATE_FC_NUM_CTX", str(REGATE_NUM_CTX)))
ADVISORY_LOG = Path(os.environ.get("GATE_ADVISORY_LOG")
                    or (Path.home() / ".ollama-dispatch" / "escalations"
                        / "ADVISORY-CAVEATS.md")).expanduser()
_GFC_MOD = None


def _gfc():
    """gate_finding_check.py from beside this file, or None (missing/broken module =
    feature off = today's behaviour; never raises)."""
    global _GFC_MOD
    if _GFC_MOD is None:
        try:
            spec = importlib.util.spec_from_file_location(
                "gate_finding_check",
                str(Path(__file__).resolve().parent / "gate_finding_check.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _GFC_MOD = mod
        except Exception as e:
            print(f"[gate] finding-check module unavailable ({type(e).__name__}: {e}) "
                  f"-- reviewer findings park as before")
            return None
    return _GFC_MOD


def _fc_enqueue(argv: list) -> tuple:
    """(ok, detail) for the verifier job's enqueue. Tests replace this; GATE_TEST_MODE
    never enqueues (-> not ok -> today's behaviour)."""
    if TEST_MODE:
        return False, "skipped (GATE_TEST_MODE)"
    e = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    return e.returncode == 0, (e.stdout if e.returncode == 0 else (e.stderr or e.stdout or ""))[-200:]


def _advisory_log(parent: str, payload: dict, rows: list, gate_json: Path) -> None:
    """Append UNCONFIRMED/advisory reviewer findings to ADVISORY_LOG (ESCALATIONS-style
    rows a human can skim; nothing reads it as state). Never raises."""
    try:
        if not rows:
            return
        if TEST_MODE and not os.environ.get("GATE_ADVISORY_LOG"):
            return   # a test must never append to the live log (found 2026-10-04)
        ADVISORY_LOG.parent.mkdir(parents=True, exist_ok=True)
        new = not ADVISORY_LOG.exists()
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        lab = payload.get("job_label") or parent
        with ADVISORY_LOG.open("a") as fh:
            if new:
                fh.write("# Advisory caveats -- reviewer findings the gate did NOT park on\n\n"
                         "Written by gate-on-complete's finding check: each row is a re-gate "
                         "reviewer finding on a green, relevance-proven verify whose reproducer "
                         "did not demonstrate it (or a concerns-only uncertain caveat). The "
                         "slice landed. Read if the area misbehaves later.\n\n")
            for r in rows:
                fh.write(f"- {ts} `{lab}` **{parent}** {r.get('finding_check')}: "
                         f"{r.get('file')}:{r.get('line')} [{r.get('reviewer_severity') or r.get('severity')}] "
                         f"{str(r.get('what') or '')[:160]} -- "
                         f"{str(r.get('finding_check_why') or '')[:200]}"
                         + (f" (reproducer: {r['reproducer']})" if r.get("reproducer") else "")
                         + f" -- {gate_json}\n")
    except Exception as e:
        print(f"[gate] {parent} advisory log write failed: {e}")


def finding_check_consider(parent: str, gate_json: Path, payload: dict, out_dir: Path,
                           report_txt: str = "") -> bool:
    """At a TERMINAL authoritative re-gate verdict: decide whether the reviewer's
    findings get the reproducer check. True = the verifier job is ENQUEUED and the
    verdict is now FC_PENDING_VERDICT -- the caller must NOT run the terminal block
    (merge_finding_check runs it when the verifier lands). False = proceed exactly as
    today (possibly with concerns-only uncertain caveats moved to advisory). Never
    raises; any failure leaves the verdict untouched."""
    before = payload.get("verdict")
    try:
        if FINDING_CHECK_MODE == "off":
            return False
        g = _gfc()
        if g is None:
            return False
        diff_path = out_dir / f"{parent}.diff"
        pl = g.plan(payload, _diff_changed_files(diff_path) if diff_path.is_file() else [])
        fc = {"plan": pl["action"], "reason": pl["reason"], "verdict_before": before}
        payload["finding_check"] = fc
        if pl["action"] == "none":
            gate_json.write_text(json.dumps(payload, indent=1))
            return False
        if pl["action"] == "advisory":
            n_adv = len(payload.get("advisory_caveats") or [])
            g.apply_outcome(payload, {n: {"outcome": "advisory", "why": pl["reason"]}
                                      for n in pl["rows"]})
            fc.update(status="done", outcome="advisory")
            gate_json.write_text(json.dumps(payload, indent=1))
            _advisory_log(parent, payload, (payload.get("advisory_caveats") or [])[n_adv:],
                          gate_json)
            print(f"[gate] {parent} finding-check: {pl['reason']} -> verdict {before} -> "
                  f"{payload.get('verdict')}")
            return False
        # action == "check"
        fcdir = out_dir / f"{parent}-fcheck"
        fcdir.mkdir(parents=True, exist_ok=True)
        ok, why = g.materialize(payload.get("job_cwd") or "",
                                (payload.get("diff_base") or (payload.get("launch_baseline") or {}).get("head") or ""),
                                diff_path, fcdir)
        if not ok:
            fc.update(status="error", error=f"materialize: {why}")
            gate_json.write_text(json.dumps(payload, indent=1))
            print(f"[gate] {parent} finding-check could not build the gated tree ({why}) "
                  f"-> verdict {before} stands")
            return False
        try:
            intent = json.loads((out_dir / f"{parent}-review" / "task.json").read_text()).get("intent", "")
        except Exception:
            intent = ""
        try:
            rtxt = report_txt or (out_dir / f"{parent}-regate" / "report.md").read_text()
        except Exception:
            rtxt = ""
        issues = payload.get("issues") or []
        rows = [{"idx": n, "severity": issues[n].get("reviewer_severity") or issues[n].get("severity"),
                 "file": issues[n].get("file"), "line": issues[n].get("line"),
                 "what": issues[n].get("what"),
                 "detail": g.finding_detail(rtxt, issues[n].get("what"))} for n in pl["rows"]]
        task = fcdir / "task.json"
        task.write_text(json.dumps({"mode": "refute", "rows": rows,
                                    "head_tree": str(fcdir / "head"),
                                    "diff": str(diff_path), "intent": intent}, indent=1))
        label = g.fc_label(parent)
        argv = ["python3", str(BIN / "ollama-queue.py"), "enqueue",
                "--model", FC_MODEL, "--host", FC_HOST, "--num-ctx", str(FC_NUM_CTX),
                "--cwd", str(fcdir), "--task-file", str(task),
                "--runner", str(BIN / "code-review-agent.py"), "--label", label,
                "--front", "--allow-no-verify"]  # proposes reproducers; the gate executes them
        _bundle = _job_field(parent, "bundle")
        if _bundle:
            argv += ["--bundle", str(_bundle)]
        ok, detail = _fc_enqueue(argv)
        if not ok:
            fc.update(status="enqueue-failed", error=str(detail)[-200:])
            gate_json.write_text(json.dumps(payload, indent=1))
            print(f"[gate] {parent} finding-check enqueue failed ({detail}) -> verdict "
                  f"{before} stands")
            return False
        fc.update(status="pending", label=label, rows=pl["rows"], model=FC_MODEL,
                  host=FC_HOST, enqueued=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        payload["verdict"] = g.FC_PENDING_VERDICT
        gate_json.write_text(json.dumps(payload, indent=1))
        print(f"[gate] {parent} finding-check ENQUEUED {label} for {len(pl['rows'])} "
              f"reviewer finding(s) on verdict {before} -- terminal decisions wait for it")
        return True
    except Exception as e:
        payload["verdict"] = before
        fc = payload.setdefault("finding_check", {})
        if isinstance(fc, dict):
            fc.update(status="error", error=f"{type(e).__name__}: {e}"[:300])
        try:
            gate_json.write_text(json.dumps(payload, indent=1))
        except Exception:
            pass
        print(f"[gate] {parent} finding-check crashed ({type(e).__name__}: {e}) -> verdict "
              f"{before} stands")
        return False


def merge_finding_check(a, out_dir: Path) -> int:
    """The verifier job (secondop-<parent>-fcheck) completed: execute its reproducers
    against the materialized trees, fold the outcome into the parent's gate.json, then
    run the terminal block merge_review deferred. ANY failure restores the verdict the
    re-gate produced (today's park). Idempotent: a record not pending is left alone."""
    g = _gfc()
    m = re.match(r"^secondop-(.+)-fcheck$", a.job_label)
    parent = (g.fc_parent(a.job_label) if g else None) or (m.group(1) if m else "")
    gate_json = out_dir / f"{parent}.gate.json"
    if not parent or not gate_json.exists():
        print(f"[gate] finding-check merge: no parent record {gate_json}")
        return 0
    payload = json.loads(gate_json.read_text())
    fc = payload.get("finding_check")
    pending = (g.FC_PENDING_VERDICT if g else "pending-finding-check")
    if not isinstance(fc, dict) or fc.get("status") != "pending" \
            or payload.get("verdict") != pending:
        print(f"[gate] {parent} finding-check merge: record not pending "
              f"(verdict={payload.get('verdict')!r}) -- nothing to do")
        return 0
    before = fc.get("verdict_before") or "fail"
    outcome, results = "error", {}
    try:
        if g is None:
            raise RuntimeError("gate_finding_check module unavailable")
        refute_p = Path(a.cwd).expanduser() / "refute.json"
        if not refute_p.is_file():
            _f = _terminal_facts(a.job_id) or {}
            raise RuntimeError(f"verifier produced no refute.json (status={_f.get('status')} "
                               f"exit={_f.get('exit_code')})")
        refute = json.loads(refute_p.read_text())
        fcdir = out_dir / f"{parent}-fcheck"
        py = Path(payload.get("job_cwd") or "/nonexistent") / ".venv" / "bin" / "python"
        results = g.check_rows(payload.get("issues") or [], fc.get("rows") or [], refute,
                               fcdir, python=str(py) if os.access(py, os.X_OK) else None,
                               deps_from=str(payload.get("job_cwd") or "") or None)
        outcome = g.overall(list(results.values()))
        fc["model_reported"] = refute.get("model")
    except Exception as e:
        fc["error"] = f"{type(e).__name__}: {e}"[:300]
        outcome = "error"
    fc["row_results"] = {str(n): {k: v for k, v in r.items() if k != "head_trace"}
                         for n, r in results.items()}
    fc["outcome"] = outcome
    fc["status"] = "done" if outcome != "error" else "error"
    payload["verdict"] = before
    n_adv = len(payload.get("advisory_caveats") or [])
    if outcome != "error":
        g.apply_outcome(payload, results)
    payload["finding_check"] = fc
    gate_json.write_text(json.dumps(payload, indent=1))
    if outcome != "error":
        _advisory_log(parent, payload, (payload.get("advisory_caveats") or [])[n_adv:], gate_json)
    print(f"[gate] {parent} finding-check {outcome.upper()}: verdict {before} -> "
          f"{payload.get('verdict')}"
          + ("" if outcome != "error" else f" (fail-safe: {fc.get('error') or 'row error'})"))
    return _finalize_review(parent, payload, gate_json, out_dir)


def _slice_ctx(job_label):
    return None     # old slice plans retired (use mini-swe): no plan context


# SOURCE-TEXT HARNESS (2026-10-05, rt-bfmr-link-sync-feedback): every review tier
# PASSed a verify.test.ts that readFileSync'd the target and asserted
# content.includes(...) / regexes over its lines -- no tier was told that a test
# which never executes the code is itself a defect. Appended to the pre-gate,
# re-gate and second-opinion contexts. Mechanically enforced too
# (source_text_harness.py in verify-quality / verify-relevance); this makes the
# reviewer catch the shapes the static check cannot see.
REVIEW_FIXTURE_RULE = (
    "\n\nTEST/FIXTURE RULE: if the diff adds or changes a test or verify fixture "
    "(verify.test.ts, test_fixture.py, *.test.*, test_*.py), check that it EXECUTES "
    "the code under test (imports/requires and calls it, renders it, or drives the "
    "real handler) and asserts observable OUTPUTS. A fixture that reads the "
    "target's source file as text (readFileSync / readFile / open().read() / "
    "read_text / inspect.getsource) and asserts on that text (includes / regex "
    ".test / match / `in` / assertIn) is a HIGH defect -- a source-text proxy: it "
    "passes a wrong implementation that contains the strings and fails a correct "
    "one written differently. Report it on the fixture file and say the cases must "
    "execute the code (literal presence belongs only in TASK.md `## Must contain`). "
    "Negated absence checks for a stated constraint (\"must not import X\") are fine.")


def _slice_review_context(job_label) -> str:
    """Extra reviewer context for a SLICE job (2026-10-03): the plan's dependents when
    this slice is intermediate, plus any concerns deferred onto it. '' otherwise --
    a non-slice job's review context is byte-identical to before."""
    try:
        ctx = _slice_ctx(job_label)
        if not ctx:
            return ""
        so = _so()
        return (so.plan_context_text(ctx)
                + so.inherited_text(so.open_obligations(ctx["run_label"], ctx["sid"],
                                                        root=_SLICE_RUNS_DIR)))
    except Exception:
        return ""


def defer_intermediate_concerns(parent, payload, gate_json) -> bool:
    """AUTHORITATIVE concerns on an INTERMEDIATE slice whose every finding the
    reviewer tagged [restored-by: <dependent>] AND that mechanically maps to that
    dependent (slice_obligations.decide_deferral) -> verdict pass, the concerns
    MOVED to open obligations on the dependents. Anything unmapped: untouched (the
    concerns verdict stands and escalates exactly as before). Returns True if deferred."""
    try:
        ctx = _slice_ctx(payload.get("job_label") or parent)
        if not ctx:
            return False
        so = _so()
        rows, why = so.decide_deferral(payload, ctx)
        if rows is None:
            if ctx.get("dependents") and payload.get("verdict") == "concerns":
                payload["deferral_refused"] = why
                gate_json.write_text(json.dumps(payload, indent=1))
            return False
        new = so.add_obligations(ctx["run_label"], ctx["sid"], parent, rows,
                                 root=_SLICE_RUNS_DIR)
        payload["verdict_before_deferral"] = payload["verdict"]
        payload["verdict"] = "pass"
        payload["deferred_concerns"] = [
            {"oid": o["oid"], "restored_by": o["restored_by"], "file": o["file"],
             "line": o["line"], "what": o["what"], "shared_terms": o["shared_terms"]}
            for o in new]
        payload["deferral_evidence"] = {
            "slice": ctx["sid"], "plan_run": ctx["run_label"],
            "job_exit_code": payload.get("job_exit_code"),
            "dependents": ctx["dependents"],
            "rule": "intermediate slice; own verify green; every concern review-sourced, "
                    "non-high, tagged [restored-by] to a transitive dependent sharing a "
                    "content term; obligations block --land-integration until discharged"}
        gate_json.write_text(json.dumps(payload, indent=1))
        print(f"[gate] {parent} INTERMEDIATE-SLICE concerns DEFERRED to "
              f"{sorted({o['restored_by'] for o in new})} as {len(new)} obligation(s) "
              f"-- verdict pass (was concerns)")
        return True
    except Exception as exc:
        print(f"[gate] {parent} deferral check failed ({type(exc).__name__}: {exc}) "
              f"-- verdict unchanged")
        return False


def discharge_slice_obligations(parent, payload, gate_json) -> None:
    """A dependent slice's terminal PASS with its own verify green discharges the
    obligations deferred onto it, evidence recorded."""
    try:
        if payload.get("verdict") != "pass" or payload.get("job_exit_code") != 0:
            return
        ctx = _slice_ctx(payload.get("job_label") or parent)
        if not ctx:
            return
        so = _so()
        if not so.open_obligations(ctx["run_label"], ctx["sid"], root=_SLICE_RUNS_DIR):
            return
        done = so.discharge(ctx["run_label"], ctx["sid"], parent, {
            "verdict": "pass", "job_exit_code": 0,
            "gate_authority": payload.get("gate_authority"),
            "review_acks": payload.get("inherited_acks") or {}}, root=_SLICE_RUNS_DIR)
        payload["obligations_discharged"] = [o["oid"] for o in done]
        gate_json.write_text(json.dumps(payload, indent=1))
        print(f"[gate] {parent} discharged {len(done)} inherited obligation(s)")
    except Exception as exc:
        print(f"[gate] {parent} obligation discharge failed ({type(exc).__name__}: {exc})")


def merge_review(a, out_dir: Path, prefix: str = "gate-",
                 authoritative: bool = False) -> int:
    """Fold a completed review job back into the parent .gate.json.

    Runs when the QUEUE finishes a review job this hook enqueued. Recomputes the
    verdict with the code findings included -- only a CODE high can fail, input
    findings still cap at concerns.

    prefix='gate-'  (authoritative=False): the cheap Unraid PRE-gate result. On a
      non-pass verdict it ESCALATES an authoritative re-gate to the Studio 27B
      (two-tier routing); a pass stands (non-terminal -- shadow signoff + Claude).
    prefix='regate-' (authoritative=True): the Studio 27B re-gate result. Its
      verdict SUPERSEDES the pre-gate's and is terminal -- it never escalates.
    """
    parent = a.job_label[len(prefix):]
    gate_json = out_dir / f"{parent}.gate.json"
    # PARENT CLEARED BEFORE ITS REVIEW LANDED (2026-10-05, regate-47d71a149da5): a
    # run-status clear / janitor stage-dedup moves <parent>.gate.json into archive/
    # while its regate is still queued (or being re-run after a harness failure).
    # The review itself already reads the archived diff (code-review-agent
    # resolve_diff_path), but this merge then said "no parent record" and DROPPED
    # the verdict -- the regate ran for nothing. Record the verdict into the
    # archived record, and run NONE of the downstream tail (autofix, pipeline
    # advance, slice feed, notify, janitor, drop): the row was cleared as handled,
    # so nothing may act on it again.
    archived_parent = False
    if not gate_json.exists():
        _alt = out_dir / "archive" / gate_json.name
        if not _alt.is_file():
            print(f"[gate] merge: no parent record {gate_json}")
            return 0
        gate_json, archived_parent = _alt, True
    payload = json.loads(gate_json.read_text())
    report = Path(a.cwd).expanduser() / "report.md"
    if not report.exists():
        # Log WHY, once, instead of only "no report". This review job is an
        # ollama-queue job (a local gate-reviewer model), NOT a headless
        # `claude -p` -- so it does not have the escalation watcher's
        # session-limit-looks-like-success bug -- but "failed (no report
        # produced)" on its own forced a human to re-derive the cause from the
        # queue log by hand (job c5013c618114, 2026-09-22).
        _why = ""
        try:
            _f = _terminal_facts(a.job_id) or {}
            _why = "status=%s exit_code=%s error=%s" % (
                _f.get("status"), _f.get("exit_code"),
                str(_f.get("error") or "")[:200])
        except Exception as _exc:
            _why = f"(could not read job facts: {_exc})"
        payload["review"] = "failed (no report produced)"
        payload["review_failed_reason"] = _why
        payload["review_failed_cwd"] = str(report)
        gate_json.write_text(json.dumps(payload, indent=1))
        print(f"[gate] {parent} review FAILED -- no report at {report} -- {_why}")
        # NEVER LEAVE 'pass-pending-review' DANGLING (2026-10-06): 25 October
        # ev-service-screen-1 author records sat at pass-pending-review forever --
        # the review job exited 0 with no report ("no reviewable changes",
        # code-review-agent since fixed), and this branch returned leaving a verdict
        # that claims a review is still coming. Resolve it: a pre-gate that produced
        # nothing escalates to the authoritative re-gate (a real review replaces the
        # missing one); an authoritative / already-regated / cleared one becomes a
        # TERMINAL 'concerns' naming the missing review, so no record reads as
        # "review pending" once no review is pending.
        if str(payload.get("verdict")) == "pass-pending-review":
            _resolve_missing_review(parent, gate_json, payload, out_dir,
                                    authoritative, archived_parent)
        return 0
    txt = report.read_text()
    m = re.search(r"^## VERDICT: (.+)$", txt, re.M)
    payload["review"] = "done"
    payload["review_verdict"] = m.group(1).strip() if m else "unknown"
    # Filter by SOURCE, not category. This dropped every code-category finding
    # before re-adding the review's rows, which was fine while 'review' was the
    # only producer of them -- it is not any more: a failed-verify finding is
    # category=code and would have been silently deleted the moment the review
    # landed, quietly restoring the pass this whole change exists to prevent.
    issues = [i for i in payload.get("issues", []) if i.get("source") != "review"]
    _uncertain = review_uncertain_claims(txt)
    for row in re.finditer(r"^\| \d+ \| (\w+) \| `([^`]+)` \| (.+?) \|$", txt, re.M):
        sev, where, what = row.group(1).lower(), row.group(2), row.group(3)
        f, _, ln = where.partition(":")
        # [restored-by: <slice-id>] (plan-context tag, slice_obligations) is lifted
        # off BEFORE the 180-char cut so truncation can never lose it.
        _rb = _RESTORED_BY_RE.search(what) if "restored" in what.lower() else None
        if _rb:
            what = _RESTORED_BY_RE.sub("", what).strip()
        _row = {"severity": sev, "file": f,
                "line": int(re.sub(r"\D", "", ln) or 0),
                "what": what.strip()[:180], "source": "review",
                "category": "code"}
        if _rb:
            _row["restored_by"] = _rb.group(1)
        if _is_uncertain_row(what, _uncertain):
            # The reviewer's OWN verifier could not settle this row (report section
            # "Uncertain -- needs a human"; review verdict PASS WITH CAVEATS). It is
            # a caveat for a human, not an established defect, so it must not count
            # as code_high and hard-FAIL the gate (s4 5983078bfe69 and 5 more on
            # 2026-10-02 failed this way). Kept visible, capped at medium.
            _row["severity"] = "medium" if sev == "high" else sev
            _row["uncertain"] = True
            _row["reviewer_severity"] = sev
        issues.append(_row)
    # Bug #14 (2026-09-18): corroborate a PRE-gate reviewer's code finding against the
    # actual diff. The Unraid 14B pre-gate has fabricated defects that cite a file NOT
    # in the diff at all -- a finding about code this change never touched is spurious
    # and must not drive an (expensive) escalation. Drop such rows at the pre-gate tier
    # only; the authoritative regate is the final word and is left intact. (A fabricated
    # claim about the BEHAVIOR of a file that IS in the diff is a separate hole, handled
    # by the strengthened regate instruction in _escalate_regate.)
    if not authoritative:
        _diff_files = _diff_changed_files(out_dir / f"{parent}.diff")
        if _diff_files:
            _kept, _dropped = [], []
            for _i in issues:
                if (_i.get("source") == "review" and _i.get("category") == "code"
                        and _i.get("file") and not _file_in_diff(_i["file"], _diff_files)):
                    _dropped.append(_i)
                else:
                    _kept.append(_i)
            if _dropped:
                payload["pregate_dropped_uncorroborated"] = [
                    {"file": d.get("file"), "what": d.get("what")} for d in _dropped]
                print(f"[gate] {parent} pre-gate: dropped {len(_dropped)} code finding(s) "
                      f"citing file(s) not in the diff (uncorroborated -- not about this change)")
                issues = _kept
        # GROUND THE LINE: a finding citing line N of a file with fewer than N lines is
        # fabricated (or mis-attributed to another file); drop it at the pre-gate tier.
        _gcwd = payload.get("job_cwd") or ""
        _kept, _dropped = [], []
        for _i in issues:
            if (_i.get("source") == "review" and _i.get("category") == "code"
                    and finding_line_out_of_range(_gcwd, _i, _diff_files or ())):
                _dropped.append(_i)
            else:
                _kept.append(_i)
        if _dropped:
            payload["pregate_dropped_ungrounded_line"] = [
                {"file": d.get("file"), "line": d.get("line"), "what": d.get("what")}
                for d in _dropped]
            print(f"[gate] {parent} pre-gate: dropped {len(_dropped)} code finding(s) "
                  f"citing a line beyond the end of the cited file (ungrounded)")
            issues = _kept
    # INHERITED OBLIGATIONS (slice_obligations): concerns an earlier slice's gate
    # deferred onto THIS slice. A reviewer NOT-RESTORED answer is a code HIGH here.
    try:
        _ictx = _slice_ctx(payload.get("job_label") or parent)
        _iobl = (_so().open_obligations(_ictx["run_label"], _ictx["sid"], root=_SLICE_RUNS_DIR)
                 if _ictx else [])
        if _iobl:
            _acks = _so().review_acks(txt)
            payload["inherited_acks"] = {o["oid"]: _acks.get(o["oid"], "unanswered")
                                         for o in _iobl}
            for o in _iobl:
                if _acks.get(o["oid"]) == "NOT-RESTORED":
                    issues.append({"severity": "high", "file": o.get("file") or "",
                                   "line": o.get("line") or 0, "source": "inherited",
                                   "category": "code",
                                   "what": (f"inherited concern {o['oid']} from "
                                            f"{o['from_slice']} NOT restored: "
                                            f"{o.get('what')}")[:180]})
    except Exception as _iexc:
        print(f"[gate] {parent} inherited-obligation check failed: {_iexc}")
    payload["issues"] = issues[:25]
    code_high = [i for i in issues if i.get("category") == "code"
                 and i["severity"] == "high"]
    payload["counts"] = {
        "code_high": len(code_high),
        "code": sum(1 for i in issues if i.get("category") == "code"),
        "input": sum(1 for i in issues if i.get("category") == "input"),
        "total": len(issues)}
    # An UNPROVEN review (the model hit its token cap and did not finish) is
    # inconclusive, not a pass: a truncated review that surfaced no HIGH row
    # must not read as a clean pass. A real code_high still fails regardless.
    _review_unproven = str(payload.get("review_verdict", "")).upper().startswith("UNPROVEN")
    payload["verdict"] = ("fail" if code_high
                          else "concerns" if (issues or _review_unproven) else "pass")
    # ESCALATION WORTHINESS: a 'concerns' verdict is only worth an authoritative
    # 27B re-gate when it carries a finding the 27B can actually adjudicate -- a
    # code-category finding, a NON-input med+ severity, or an UNPROVEN (truncated)
    # review that never finished. A concerns verdict made entirely of low
    # input/scope flags ("this edited file was not named by the task; confirm it
    # was needed") is NOT one of those: the 27B re-asks the identical scope
    # question the coordinator answers at the merge diff-glance, adding no signal
    # while burning a Studio slot. Those stand as terminal 'concerns'.
    #
    # THE MED+ CLAUSE MUST EXCLUDE input findings the 27B CANNOT ADJUDICATE
    # (2026-09-11, job fd7b1d7cd7d6). The 27B reviews the DIFF; a completeness /
    # verify-quality concern is about the TASK/VERIFY TEXT, not the diff, so the
    # 27B comes back PASS "nothing survived", having burned a Studio slot.
    # fd7b1d7cd7d6's ONLY finding was severity=medium category=input
    # source=completeness ("Must-contain lists `Bearer` which already existed in
    # baseline") and the old clause (ANY med+ regardless of category/source)
    # escalated it needlessly.
    #
    # NOT a blanket `category != "input"` exclusion: scope findings are ALSO
    # category=input in gate.py, and one of them -- the MEDIUM "the diff edits the
    # VERIFY/CHECK that gated it" selftest finding (gate.py) -- IS diff-visible and
    # exactly the kind of thing a 27B should look at. So exclude by SOURCE: a med+
    # input finding escalates UNLESS its source is a task/verify-spec source the
    # diff reviewer can't adjudicate. code findings (any severity) still escalate
    # via _code_findings; scope med+ still escalates; an unproven review still
    # escalates.
    _NON_ADJUDICABLE = {"completeness", "verify-quality", "launch-baseline"}
    _med_plus = [i for i in issues if i.get("severity") in ("high", "medium")
                 and not (i.get("category") == "input"
                          and i.get("source") in _NON_ADJUDICABLE)]
    _code_findings = [i for i in issues if i.get("category") == "code"]
    _escalation_worthy = bool(_code_findings or _med_plus or _review_unproven)
    # DETERMINISTIC FAIL (2026-10-02, bc15112d643a): "the job's own verify FAILED" is
    # a fact, not an opinion -- the verdict recompute keeps it code_high whatever a
    # reviewer says, so an authoritative re-gate cannot change the outcome. It only
    # burned a Studio slot and, enqueued --front, PREEMPTED the live continuation
    # round of the same bundle (c1 76ee077e738e paused by SIGTERM). Not escalated.
    _deterministic_fail = any(i.get("source") == "verify-exit" and i.get("severity") == "high"
                              for i in issues)
    # not_checked was written by the decidable pass and MUST survive the merge:
    # the review landing does not retroactively check what scope or completeness
    # abstained on, and this is the render most likely to be read as final.
    nc = payload.get("not_checked") or []
    nc_s = (" not_checked=" + ",".join(x.split(" (")[0] for x in nc)) if nc else ""
    # An UNTRUSTED input survives the review landing untouched: a model reading
    # the diff cannot un-contaminate the tree that diff came from. The verdict
    # recomputed above is issue-driven, and the dirty-baseline finding is an
    # input issue that stays in payload["issues"], so a pass is already
    # impossible here -- this only keeps the reason visible.
    ut = " UNTRUSTED=" + ";".join(payload["untrusted"]) if payload.get("untrusted") else ""
    gate_json.write_text(json.dumps(payload, indent=1))
    tier = "regate(27b,authoritative)" if authoritative else "pregate(14b)"
    print(f"[gate] {parent} verdict={payload['verdict']} "
          f"code_high={len(code_high)}{nc_s}{ut} (review merged, {tier})")
    if archived_parent:
        if authoritative:
            payload["gate_authority"] = "studio-27b-regate"
            payload["regate"] = "done"
        payload["merged_after_clear"] = True
        gate_json.write_text(json.dumps(payload, indent=1))
        print(f"[gate] {parent} parent was CLEARED (record in archive/) -- verdict "
              f"recorded there; no downstream actions")
        return 0
    if authoritative:
        # The Studio 27B verdict is the authority: mark it, and never escalate
        # again (this branch is only reached for a 'regate-' completion).
        payload["gate_authority"] = "studio-27b-regate"
        payload["regate"] = "done"
        gate_json.write_text(json.dumps(payload, indent=1))
        if payload.get("verdict") == "concerns":
            defer_intermediate_concerns(parent, payload, gate_json)
        # GATE FINDING CHECK: a reviewer finding on a green, relevance-proven verify is
        # checked by an executed reproducer before it can park the slice. While the
        # verifier job is pending the verdict is FC_PENDING_VERDICT and the terminal
        # block below is deferred to merge_finding_check.
        if finding_check_consider(parent, gate_json, payload, out_dir, txt):
            return 0
    elif (TWO_TIER and payload.get("verdict") == "pass" and not payload.get("regate")
          and _pass_needs_regate(out_dir / f"{parent}.diff")):
        # PASS RE-CHECK (operator 2026-10-01, Studio is the only local lane now): the Unraid
        # qwen3:14b pre-gate at a 6144 ctx used to be the SOLE decider on a PASS -- the
        # 35B regate only ever saw FAILs, so it could turn a FAIL into a PASS but never the
        # reverse. A PASS on a substantial code diff is now re-reviewed by the 35B; its
        # verdict supersedes (and can overturn) the pre-gate's.
        print(f"[gate] {parent} pre-gate PASS on a substantial code diff -> authoritative re-check")
        _escalate_regate(parent, gate_json, payload, out_dir)
    elif TWO_TIER and payload.get("verdict") != "pass" and not payload.get("regate") \
            and _deterministic_fail:
        payload["regate"] = "not-warranted (deterministic verify failure -- no review can overturn it)"
        payload["gate_authority"] = "pregate-terminal-verify-exit"
        gate_json.write_text(json.dumps(payload, indent=1))
        print(f"[gate] {parent} the job's own verify FAILED -> NO re-gate "
              f"(a review cannot overturn a verify failure)")
    elif TWO_TIER and payload.get("verdict") != "pass" and not payload.get("regate"):
        # TWO-TIER ESCALATION: the Unraid pre-gate is a cheap trigger, not the
        # authority. A non-pass pre-gate verdict escalates an authoritative
        # re-gate to the Studio 27B ONLY when the concerns are worth adjudicating
        # (a code-category finding, a med+ severity, or an UNPROVEN review); its
        # verdict will supersede this one when it lands. A pre-gate PASS is left
        # to stand (non-terminal: shadow signoff holds for the human/Claude, and
        # Claude reads the full diff at merge).
        if _escalation_worthy:
            _escalate_regate(parent, gate_json, payload, out_dir)
        else:
            # Concerns are low-severity input/scope only -- no code finding, no
            # med+, review completed. Not worth a 27B slot: it would re-ask the
            # same "was this edited file needed?" question the coordinator
            # answers at the merge diff-glance. Verdict stands as terminal
            # 'concerns'; the coordinator reviews the diff. This is the
            # over-trigger that congested the Studio lane with needless regates.
            payload["regate"] = "not-warranted (low input/scope only)"
            payload["gate_authority"] = "pregate-terminal-lowscope"
            gate_json.write_text(json.dumps(payload, indent=1))
            print(f"[gate] {parent} concerns are low input/scope only "
                  f"({payload['counts']['input']} input, 0 code, 0 med+) "
                  f"-> NO re-gate (coordinator reviews the diff)")
    return _finalize_review(parent, payload, gate_json, out_dir)


REVIEW_MISSING_ISSUE = {
    "severity": "medium", "file": "", "line": 0, "source": "review-missing",
    "category": "input",
    "what": "the code review finished without producing a report -- the diff was "
            "never reviewed (missing measurement, not a code finding)"}


def _resolve_missing_review(parent: str, gate_json: Path, payload: dict, out_dir: Path,
                            authoritative: bool, archived_parent: bool) -> None:
    """A review job finished with NO report while the record still says
    'pass-pending-review'. Escalate a pre-gate to the authoritative re-gate once;
    otherwise record a terminal 'concerns' with a review-missing input issue. Runs
    no autofix/pipeline tail (unchanged from before: a missing review is not a
    finding to fix). Advisory; never raises."""
    try:
        if (not authoritative and TWO_TIER and not archived_parent
                and not payload.get("regate")):
            payload["review_missing"] = "pregate"
            _escalate_regate(parent, gate_json, payload, out_dir)
            return
        payload["review_missing"] = "authoritative" if authoritative else "terminal"
        issues = [i for i in payload.get("issues", []) if i.get("source") != "review-missing"]
        issues.append(dict(REVIEW_MISSING_ISSUE))
        payload["issues"] = issues[:25]
        c = payload.get("counts") or {}
        payload["counts"] = {
            "code_high": c.get("code_high", 0), "code": c.get("code", 0),
            "input": sum(1 for i in issues if i.get("category") == "input"),
            "total": len(issues)}
        payload["verdict"] = "concerns"
        payload["gate_authority"] = "review-missing-terminal"
        gate_json.write_text(json.dumps(payload, indent=1))
        print(f"[gate] {parent} review produced no report -> terminal 'concerns' "
              f"(review-missing; was pass-pending-review)")
    except Exception as _e:
        print(f"[gate] {parent} could not resolve the missing review: {_e}")


def _finalize_review(parent: str, payload: dict, gate_json: Path, out_dir: Path) -> int:
    """The post-verdict tail of merge_review (second opinion, notify, re-signoff and,
    at a terminal verdict, the autofix/triage/pipeline/slice/janitor/drop block).
    Factored out VERBATIM (2026-10-04) so merge_finding_check can run it once the
    verifier lands; behaviour for every other path is unchanged."""
    # CROSS-FAMILY SECOND OPINION on any PASS (operator 2026-10-01: a PASS must not be
    # decided by one model family). Fires at whichever tier produced the standing
    # PASS -- a pre-gate PASS that needs no re-gate, or an authoritative re-gate
    # PASS (including one that OVERTURNED a pre-gate non-pass: that PASS is
    # Qwen-only too). Skipped while regate=="pending": the verdict is not settled
    # yet and the authoritative merge will come back through here. Purely
    # ADDITIVE -- see request_second_opinion / merge_second_opinion. Advisory and
    # best-effort: nothing below waits on it and it cannot alter the verdict, so a
    # failure here must not disturb the gate.
    try:
        if payload.get("verdict") == "pass" and payload.get("regate") != "pending":
            request_second_opinion(parent, gate_json, payload, out_dir)
    except Exception as _so_exc:
        print(f"[gate] {parent} second opinion request failed "
              f"({type(_so_exc).__name__}: {_so_exc}) -- verdict unaffected")
    # RE-DECIDE THE SIGN-OFF NOW THAT THE REVIEW HAS LANDED. At emit time the
    # review was still queued, so the record said pass-pending-review and
    # review_verdict='not-run' -- two of auto_decide's conditions -- and the
    # harness held for a human on EVERY job. Without this re-run autonomous
    # sign-off could never fire in production, whatever the evidence said. In
    # shadow mode this only RECORDS a shadow_decision (verdict stays None), so a
    # pre-gate that escalated still holds for its authoritative 27B re-gate.
    # QUIET-FAIL CLOSE (finding #3). The merge is where the model review's code
    # findings actually arrive, so this -- not emit() -- is where a
    # pass-pending-review flips to 'fail' with code_high>=1. emit()'s notifier
    # never saw that verdict (the review was still queued then), so fire it here
    # on any non-pass. Advisory and best-effort (rule 2): never affects the exit
    # code. Addressed to the PARENT dispatch (its launched_by is on the parent
    # row/sidecar, not the gate job's). An authoritative regate re-notifying a
    # standing pre-gate non-pass is intended -- it is the confirmed verdict.
    try:
        if str(payload.get("verdict")) not in ("pass", "skipped", "pass-pending-review") \
                and not TEST_MODE:
            _notify_non_pass(parent, payload, gate_json)
    except Exception:
        pass
    resignoff(parent, gate_json, payload, out_dir / f"{parent}.diff")
    # AUTO-FIX decision, ONLY at a TERMINAL verdict. A pre-gate non-pass that just
    # ESCALATED (regate=="pending") is NOT terminal -- the authoritative 27B
    # verdict is still coming, and deciding now would classify off the cheap
    # pre-gate and could double-fire when the regate lands. So gate on "not still
    # waiting on a regate": an authoritative regate completion (regate=="done"),
    # a pre-gate PASS that stands (no regate), or a pre-gate concerns marked
    # terminal (regate "not-warranted..."/"enqueue-failed"/"skipped...").
    if payload.get("regate") != "pending":
        autofix_consider(parent, payload, gate_json, out_dir)
        # ESCALATION TRIAGE (Task E): a reviewer-code-high park gets its
        # intended-removal analysis attached immediately. Shadow by default.
        escalation_triage_consider(parent, payload, gate_json)
        # SUCCESS-path mirror (Task C): a CLEAN pass advances the pipeline (an
        # authoring harness -> coding job; a coding PASS -> apply). OFF by default.
        auto_pipeline_consider(parent, payload, gate_json)
        auto_land_consider(parent, payload, gate_json)
        # Obligations deferred onto this slice are discharged by its green PASS.
        discharge_slice_obligations(parent, payload, gate_json)
        # SLICE AUTO-FEED: a completed slice job advances its own plan (detached).
        slice_pipeline_consider(parent, payload, gate_json)
        # RUN-STATUS JANITOR: auto-archive safe rows + superseded stages. Shadow.
        runstatus_janitor_consider(parent, payload, gate_json)
    # COMPLETED-CODE-DROP at the terminal verdict (same guard as auto-fix: not
    # still waiting on a regate). An authoritative regate that supersedes a
    # standing pre-gate re-drops and moves the folder between Pass/ and Failed/.
    try:
        if payload.get("regate") != "pending":
            drop_completed_code(parent, payload, out_dir)
    except Exception:
        pass
    return 0


PASS_REGATE_MIN_LINES = int(os.environ.get("GATE_PASS_REGATE_MIN_LINES", "30"))
_CODE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".swift",
              ".java", ".kt", ".c", ".cc", ".cpp", ".h", ".rb", ".php", ".sh", ".cs"}


def _pass_needs_regate(diff_path) -> bool:
    """True when a pre-gate PASS should still get the authoritative 35B re-check: the
    diff touches at least one code file and changes more than PASS_REGATE_MIN_LINES
    lines (added + removed, test files included -- a wrong test is also a defect).
    Unreadable/missing diff -> False (never invent work). 0 disables the feature."""
    if PASS_REGATE_MIN_LINES <= 0:
        return False
    try:
        txt = Path(diff_path).read_text(errors="replace")
    except Exception:
        return False
    code, changed = False, 0
    for ln in txt.splitlines():
        if ln.startswith("+++ b/"):
            code = code or Path(ln[6:].strip()).suffix.lower() in _CODE_EXTS
        elif ln.startswith(("+", "-")) and not ln.startswith(("+++", "---")):
            changed += 1
    return code and changed > PASS_REGATE_MIN_LINES


def _escalate_regate(parent: str, gate_json: Path, payload: dict,
                     out_dir: Path) -> None:
    """Enqueue the authoritative Studio-27B re-gate for a non-pass pre-gate
    verdict. Distinct 'regate-' label so it never re-fires the 'gate-' loop
    guard. Reuses the pre-gate's diff + intent. Advisory; never raises."""
    review_task = out_dir / f"{parent}-review" / "task.json"
    diff_path = out_dir / f"{parent}.diff"
    regate_dir = out_dir / f"{parent}-regate"
    regate_dir.mkdir(parents=True, exist_ok=True)
    try:
        base = json.loads(review_task.read_text()) if review_task.exists() else {}
    except Exception:
        base = {}
    # DEFECT-2 FIX: the re-gate reviews the diff, but nothing told it WHICH files
    # the pre-gate actually raised findings on -- so a HIGH on a helper file
    # (lib/bfmrSalePrice.ts, this session) could return PASS while the report
    # only ever discussed the declared target route. Fold the pre-gate's flagged
    # files + their findings into the review context and REQUIRE the re-gate to
    # explicitly confirm or refute each one BY FILE. The reviewer already reads
    # `context` (code-review-agent honours mode/diff/intent/context), so this
    # needs no runner change. `issues` carry {severity,file,line,what}.
    _flagged: dict[str, list[str]] = {}
    for _i in payload.get("issues", []):
        _f = _i.get("file")
        if not _f:
            continue
        _sev = str(_i.get("severity", "?")).upper()
        _ln = _i.get("line") or ""
        _what = str(_i.get("what", "")).strip()
        _loc = f"{_f}:{_ln}" if _ln else _f
        _flagged.setdefault(_f, []).append(
            f"[{_sev}] {_loc} -- {_what}" if _what else f"[{_sev}] {_loc}")
    _ctx = ("AUTHORITATIVE re-gate on the local Darkbloom model: the Unraid pre-gate "
            f"returned {payload.get('verdict') or 'non-pass'}. Report only defects this change introduces.")
    _ctx += REVIEW_FIXTURE_RULE
    _ctx += _slice_review_context(payload.get("job_label") or parent)
    if _flagged:
        _lines = []
        for _f in sorted(_flagged):
            _lines.append(f"  - {_f}")
            for _msg in _flagged[_f][:5]:
                _lines.append(f"      {_msg}")
        _ctx += (
            "\n\nThe pre-gate raised findings on the file(s) below. You MUST "
            "review EACH of these files' changes specifically and, for each, "
            "state in your report whether the finding is CONFIRMED or REFUTED "
            "(with the reason). Do not return PASS while any flagged file is "
            "unaddressed -- a PASS that never mentions a flagged file is not a "
            "clearance of it.\n" + "\n".join(_lines))
        # Bug #14 (2026-09-18): the pre-gate reviewer has FABRICATED a defect by
        # asserting a behavior the code does not have (e.g. claiming "'   '.strip()
        # counts as filled" when the code correctly treats it as missing). A finding
        # is only real if the CODE ACTUALLY BEHAVES as claimed -- the quoted code
        # merely existing in the diff is NOT evidence. Force the authoritative
        # reviewer to re-derive each claimed behavior from the real control flow.
        _ctx += (
            "\n\nCRITICAL -- do NOT accept a pre-gate finding on the strength of a "
            "quoted line existing. A finding asserts a BEHAVIOR (\"this code does X\"); "
            "REFUTE it unless you can trace the actual control/data flow in the diff "
            "and confirm the code truly does X for the stated input. Re-derive the "
            "outcome yourself; treat the pre-gate's claimed outcome as unverified. "
            "If the code demonstrably does the OPPOSITE of the claim (a common "
            "fabrication is inverting a condition), mark the finding REFUTED and say so.")
    (regate_dir / "task.json").write_text(json.dumps({
        "mode": "review", "diff": base.get("diff", str(diff_path)),
        "intent": base.get("intent", ""),
        "context": _ctx,
        # Let the verifier read helpers the hunk calls/removes (outside the diff).
        "repo": payload.get("job_cwd") or base.get("repo") or "",
        "ref_budget": ref_budget_for(REGATE_NUM_CTX),
        "base": payload.get("diff_base") or (payload.get("launch_baseline") or {}).get("head") or base.get("base") or ""},
        indent=1))
    label = f"regate-{parent}"
    payload["pregate_verdict"] = payload.get("verdict")
    payload["pregate_review_verdict"] = payload.get("review_verdict")
    enq = ["python3", str(BIN / "ollama-queue.py"), "enqueue",
           "--model", REGATE_MODEL, "--host", REGATE_HOST,
           "--num-ctx", str(REGATE_NUM_CTX),
           "--cwd", str(regate_dir), "--task-file", str(regate_dir / "task.json"),
           "--runner", str(BIN / "code-review-agent.py"), "--label", label, "--front",
           "--allow-no-verify"]  # advisory scope/diff review: no --verify by design
    if TEST_MODE:
        payload["regate"] = "skipped (GATE_TEST_MODE)"
        payload["regate_label"] = label
        gate_json.write_text(json.dumps(payload, indent=1))
        return
    try:
        e = subprocess.run(enq, capture_output=True, text=True, timeout=120)
        payload["regate"] = "pending" if e.returncode == 0 else "enqueue-failed"
        payload["regate_label"] = label
        if e.returncode != 0:
            payload["regate_error"] = (e.stderr or e.stdout or "")[-200:]
    except Exception as ex:
        payload["regate"] = "enqueue-failed"
        payload["regate_error"] = f"{type(ex).__name__}: {str(ex)[:160]}"
    gate_json.write_text(json.dumps(payload, indent=1))
    print(f"[gate] {parent} pre-gate={payload.get('pregate_verdict')} -> ESCALATED "
          f"re-gate to {REGATE_MODEL}@{REGATE_HOST} (label {label}, "
          f"status={payload['regate']})")


def _model_family(model: str) -> str:
    """Coarse model-family token, for the cross-family assertion below.

    Not a general taxonomy -- just enough to tell "is this the SAME lineage as
    the qwen tiers?" apart from "is this something else?". Strips the tag, the
    registry/user prefix and any size/quant suffix, then keeps the leading
    alphabetic run: qwen3:14b -> 'qwen', qwen3.6-35b-a3b-vl-mtp-mxfp8 -> 'qwen',
    gemma4:12b-it-q4_K_M -> 'gemma', phi4:14b -> 'phi', mistral:latest ->
    'mistral', hf.co/unsloth/Seed-Coder-8B... -> 'seed'.
    """
    s = str(model or "").split(":", 1)[0].rsplit("/", 1)[-1].lower()
    out = []
    for ch in s:
        if ch.isalpha():
            out.append(ch)
        else:
            break
    return "".join(out)


def _second_opinion_eligible(payload: dict, diff_path) -> tuple[bool, str]:
    """Should a second opinion fire for this (PASSing) gate record?

    PURE -- no enqueue, no I/O beyond reading the diff -- so --self-test drives
    the exact predicate the daemon path uses. Returns (fire?, reason).

    CROSS-FAMILY IS A HARD PRECONDITION, not a config convenience: the entire
    point is that a PASS is not decided by one family. If GATE_SECOND_OPINION_MODEL
    is (mis)set to another qwen, firing it would burn a slot and -- worse -- stamp
    the record with a cross-family corroboration it did not get. Refuse instead,
    loudly, and say why in the record.
    """
    if not SECOND_OPINION:
        return False, "disabled (GATE_SECOND_OPINION=0)"
    if str(payload.get("verdict")) != "pass":
        return False, f"verdict={payload.get('verdict')} (only a PASS needs corroborating)"
    if payload.get("second_opinion_label"):
        return False, "already requested"
    fam = _model_family(SECOND_OPINION_MODEL)
    same = {f for f in (_model_family(PREGATE_MODEL), _model_family(REGATE_MODEL)) if f}
    if not fam:
        return False, f"cannot determine family of {SECOND_OPINION_MODEL!r}"
    if fam in same:
        return False, (f"REFUSED: {SECOND_OPINION_MODEL} is family {fam!r}, the SAME as a "
                       f"gate tier ({'/'.join(sorted(same))}) -- a same-family review is "
                       f"not a second opinion")
    code, changed = False, 0
    try:
        for ln in Path(diff_path).read_text(errors="replace").splitlines():
            if ln.startswith("+++ b/"):
                code = code or Path(ln[6:].strip()).suffix.lower() in _CODE_EXTS
            elif ln.startswith(("+", "-")) and not ln.startswith(("+++", "---")):
                changed += 1
    except Exception:
        return False, "no readable diff"
    if not code:
        return False, "no code file in the diff"
    if changed < SECOND_OPINION_MIN_LINES:
        return False, f"{changed} changed lines < GATE_SECOND_OPINION_MIN_LINES={SECOND_OPINION_MIN_LINES}"
    return True, f"code diff, {changed} changed lines"


def request_second_opinion(parent: str, gate_json: Path, payload: dict,
                           out_dir: Path) -> None:
    """Enqueue the cross-family Unraid second-opinion review of a PASSing diff.

    Additive by construction: writes only second_opinion* keys. Label
    'secondop-<parent>' is disjoint from 'gate-'/'regate-' so it never re-fires
    either loop. Advisory; never raises."""
    fire, why = _second_opinion_eligible(payload, out_dir / f"{parent}.diff")
    if not fire:
        if why.startswith("REFUSED") or why.startswith("cannot determine"):
            payload["second_opinion"] = {"review": "not-run", "reason": why}
            gate_json.write_text(json.dumps(payload, indent=1))
            print(f"[gate] {parent} second opinion {why}")
        return
    review_task = out_dir / f"{parent}-review" / "task.json"
    diff_path = out_dir / f"{parent}.diff"
    so_dir = out_dir / f"{parent}-secondop"
    so_dir.mkdir(parents=True, exist_ok=True)
    try:
        base = json.loads(review_task.read_text()) if review_task.exists() else {}
    except Exception:
        base = {}
    ctx = (
        "INDEPENDENT SECOND OPINION. Another reviewer (a different model family) "
        "already reviewed this diff and returned PASS. You are being asked because "
        "a PASS decided by a single model family can hide a shared blind spot.\n\n"
        "Review the diff YOURSELF, from scratch. You have NOT been told what the "
        "other reviewer looked at and you must not assume it was thorough; equally, "
        "do not invent a defect to justify being asked -- returning PASS is a "
        "perfectly good answer and is the expected one most of the time.\n\n"
        "Report ONLY defects this change INTRODUCES, and only ones you can trace in "
        "the actual control/data flow of the diff. A line merely existing is not a "
        "defect. Do not report style, naming or test-coverage preferences."
        + REVIEW_FIXTURE_RULE)
    (so_dir / "task.json").write_text(json.dumps({
        "mode": "review", "diff": base.get("diff", str(diff_path)),
        "intent": base.get("intent", ""), "context": ctx}, indent=1))
    label = f"secondop-{parent}"
    enq = ["python3", str(BIN / "ollama-queue.py"), "enqueue",
           "--model", SECOND_OPINION_MODEL, "--host", SECOND_OPINION_HOST,
           "--num-ctx", str(SECOND_OPINION_NUM_CTX),
           "--cwd", str(so_dir), "--task-file", str(so_dir / "task.json"),
           "--runner", str(BIN / "code-review-agent.py"), "--label", label,
           "--allow-no-verify"]  # advisory diff review: no --verify by design
    # Deliberately NOT --front: this is advisory corroboration of a PASS that
    # already stands. It must never jump ahead of real dispatch work, and nothing
    # downstream blocks on it.
    payload["second_opinion_label"] = label
    payload["second_opinion"] = {
        "review": "pending", "model": SECOND_OPINION_MODEL,
        "host": SECOND_OPINION_HOST, "num_ctx": SECOND_OPINION_NUM_CTX,
        "family": _model_family(SECOND_OPINION_MODEL),
        "corroborates_verdict": payload.get("verdict"),
        "authority": "advisory-only (can flag, can never flip a verdict)",
        "why": why}
    if TEST_MODE:
        payload["second_opinion"]["review"] = "skipped (GATE_TEST_MODE)"
        gate_json.write_text(json.dumps(payload, indent=1))
        return
    try:
        e = subprocess.run(enq, capture_output=True, text=True, timeout=120)
        payload["second_opinion"]["review"] = (
            "pending" if e.returncode == 0 else "enqueue-failed")
        if e.returncode != 0:
            payload["second_opinion"]["error"] = (e.stderr or e.stdout or "")[-200:]
            payload.pop("second_opinion_label", None)
    except Exception as ex:
        payload["second_opinion"]["review"] = "enqueue-failed"
        payload["second_opinion"]["error"] = f"{type(ex).__name__}: {str(ex)[:160]}"
        payload.pop("second_opinion_label", None)
    gate_json.write_text(json.dumps(payload, indent=1))
    print(f"[gate] {parent} PASS -> cross-family SECOND OPINION on "
          f"{SECOND_OPINION_MODEL}@{SECOND_OPINION_HOST} (label {label}, "
          f"status={payload['second_opinion']['review']}, advisory: flags only)")


def merge_second_opinion(a, out_dir: Path) -> int:
    """Fold a finished 'secondop-' review into the parent record AS A FLAG.

    This function must never touch payload["verdict"], ["issues"], ["counts"],
    ["gate_authority"], the signoff or the completed-code drop. If you are
    extending it and reach for one of those, the answer is no -- route the
    disagreement through second_opinion_flags and let a human decide."""
    parent = a.job_label[len("secondop-"):]
    gate_json = out_dir / f"{parent}.gate.json"
    if not gate_json.exists():
        print(f"[gate] second opinion: no parent record {gate_json}")
        return 0
    payload = json.loads(gate_json.read_text())
    so = dict(payload.get("second_opinion") or {})
    report = Path(a.cwd).expanduser() / "report.md"
    skipped = Path(a.cwd).expanduser() / "skipped.json"
    if not report.exists() and skipped.exists():
        # The review SKIPPED cleanly (its diff was gone, 2026-10-02
        # secondop-d4e286641d56): a missing measurement, never a code concern.
        try:
            _r = json.loads(skipped.read_text()).get("reason") or "skipped"
        except Exception:
            _r = "skipped"
        so.update({"review": "not-run", "reason": _r})
        payload["second_opinion"] = so
        payload["second_opinion_agreement"] = "unknown (second opinion not run: diff unavailable)"
        gate_json.write_text(json.dumps(payload, indent=1))
        print(f"[gate] {parent} second opinion NOT RUN ({_r}) -- verdict unchanged "
              f"({payload.get('verdict')})")
        return 0
    if not report.exists():
        _why = ""
        try:
            _f = _terminal_facts(a.job_id) or {}
            _why = "status=%s exit_code=%s error=%s" % (
                _f.get("status"), _f.get("exit_code"), str(_f.get("error") or "")[:200])
        except Exception as _exc:
            _why = f"(could not read job facts: {_exc})"
        so.update({"review": "failed (no report produced)", "reason": _why})
        payload["second_opinion"] = so
        # A failed second opinion is NOT a concern about the code -- it is a
        # missing measurement. Say so where a human will read it, and change
        # nothing about the verdict.
        payload["second_opinion_agreement"] = "unknown (review did not produce a report)"
        gate_json.write_text(json.dumps(payload, indent=1))
        print(f"[gate] {parent} second opinion FAILED (no report at {report}) -- "
              f"{_why} -- verdict unchanged ({payload.get('verdict')})")
        return 0
    txt = report.read_text()
    m = re.search(r"^## VERDICT: (.+)$", txt, re.M)
    verdict = m.group(1).strip() if m else "unknown"
    rows = []
    for row in re.finditer(r"^\| \d+ \| (\w+) \| `([^`]+)` \| (.+?) \|$", txt, re.M):
        sev, where, what = row.group(1).lower(), row.group(2), row.group(3)
        f, _, ln = where.partition(":")
        rows.append({"severity": sev, "file": f,
                     "line": int(re.sub(r"\D", "", ln) or 0),
                     "what": what.strip()[:180], "source": "second-opinion",
                     "category": "code"})
    # CORROBORATION, same rule the pre-gate gets (bug #14): a finding citing a
    # file this diff never touched is not about this change. Drop it rather than
    # flag a human with it.
    dropped = []
    _pd = out_dir / f"{parent}.diff"
    if not _pd.is_file() and (out_dir / "archive" / _pd.name).is_file():
        _pd = out_dir / "archive" / _pd.name     # parent cleared -> sidecars archived
    diff_files = _diff_changed_files(_pd)
    if diff_files:
        kept = []
        for i in rows:
            if i.get("file") and not _file_in_diff(i["file"], diff_files):
                dropped.append(i)
            else:
                kept.append(i)
        rows = kept
    med_plus = [i for i in rows if i.get("severity") in ("high", "medium")]
    disagrees = bool(med_plus) or not str(verdict).upper().startswith("PASS")
    so.update({"review": "done", "verdict": verdict,
               "counts": {"total": len(rows),
                          "high": sum(1 for i in rows if i["severity"] == "high"),
                          "med_plus": len(med_plus)},
               "dropped_uncorroborated": [
                   {"file": d.get("file"), "what": d.get("what")} for d in dropped]})
    payload["second_opinion"] = so
    payload["second_opinion_issues"] = rows[:25]
    payload["second_opinion_agreement"] = "disagree" if disagrees else "agree"
    if disagrees:
        payload["second_opinion_disagreement"] = True
        flags = list(payload.get("second_opinion_flags") or [])
        flags.append(
            f"CROSS-FAMILY DISAGREEMENT: {so['model']} ({so.get('family')}) returned "
            f"{verdict} on a diff the qwen gate PASSED ({len(med_plus)} med+/"
            f"{len(rows)} findings). Verdict NOT changed -- read the diff.")
        for i in rows[:10]:
            flags.append(f"  [{i['severity'].upper()}] "
                         f"{i['file']}{':' + str(i['line']) if i['line'] else ''} -- {i['what']}")
        payload["second_opinion_flags"] = flags
    gate_json.write_text(json.dumps(payload, indent=1))
    print(f"[gate] {parent} second opinion ({so['model']}) verdict={verdict} -> "
          f"{payload['second_opinion_agreement']}; parent verdict UNCHANGED "
          f"({payload.get('verdict')})"
          + (f" -- {len(med_plus)} med+ finding(s) FLAGGED for the coordinator"
             if disagrees else ""))
    # The second opinion is the LAST condition auto-land waits on: re-evaluate now.
    try:
        auto_land_consider(parent, payload, gate_json)
    except Exception:
        pass
    return 0


def resignoff(job_id: str, gate_json: Path, payload: dict, diff_path: Path) -> None:
    """Run signoff.py --auto against the FINISHED gate record and mirror the
    outcome into it. Advisory; never raises."""
    if not payload.get("signoff_required") or payload.get("signoff_verdict"):
        return
    try:
        r = subprocess.run(
            ["python3", str(SIGNOFF), "--auto", job_id, "--gate", str(gate_json),
             "--diff", str(diff_path)],
            capture_output=True, text=True, timeout=60)
        if r.stdout:
            print(r.stdout, end="")
        so = _signoff_record(job_id)
        payload["signoff_verdict"] = so.get("verdict")
        payload["signoff_reviewer"] = so.get("reviewer", "")
        payload.pop("signoff_auto_blocked_by", None)
        if so.get("auto_blocked_by"):
            payload["signoff_auto_blocked_by"] = so["auto_blocked_by"]
        if so.get("shadow_decision"):
            payload["signoff_shadow_decision"] = so["shadow_decision"]
            payload["signoff_shadow_reasons"] = (so.get("shadow_reasons") or [])[:6]
        payload["signoff_redecided_after_review"] = True
        gate_json.write_text(json.dumps(payload, indent=1))
    except Exception as e:
        payload["signoff_error"] = f"re-decide: {type(e).__name__}: {e}"[:200]
        try:
            gate_json.write_text(json.dumps(payload, indent=1))
        except Exception:
            pass


QUEUE_STATE = STATE_DIR / "ollama-queue-state.json"


def _completion_record(job_id: str) -> dict | None:
    """The queue's DURABLE per-job completion sidecar, written by ollama-queue.py
    at gate-fire time (_persist_job_completion).

    WHY IT EXISTS. QUEUE_STATE prunes finished jobs immediately
    (RETAIN_DONE_RECENT = 0): by the time this fire-and-forget hook runs, the
    live row is usually gone, so exit_code and launch_baseline are unreadable
    from it and the gate abstains on both (not_checked). This file is written
    from the still-complete job dict and is never pruned, so the readers below
    fall back to it and can certify those facts. Fail-open, exactly like the live
    readers: a torn or missing file abstains rather than throwing."""
    try:
        p = STATE_DIR / "ollama-queue-logs" / f"{job_id}.done.json"
        if p.exists():
            rec = json.loads(p.read_text())
            return rec if isinstance(rec, dict) else None
    except Exception:
        return None
    return None


def _live_job(job_id: str) -> dict | None:
    """The current queue row for a job off ollama-queue-state.json, or None.

    The single place the live state file is read and scanned -- launch_baseline,
    job_verify_exit, job_facts and _job_field all route through here (and through
    _terminal_facts) instead of each re-implementing the same load+iterate loop.
    Fail-open: a torn read (the daemon writes this file concurrently) or a
    missing file abstains rather than throwing (rule 2)."""
    try:
        jobs = json.loads(QUEUE_STATE.read_text()).get("jobs") or []
        if isinstance(jobs, dict):
            jobs = list(jobs.values())
        for j in jobs:
            if j.get("id") == job_id:
                return j if isinstance(j, dict) else None
    except Exception:
        return None
    return None


def _terminal_facts(job_id: str) -> dict | None:
    """The job's TERMINAL facts, SIDECAR-FIRST. A job-record-shaped dict or None.

    THE RACE THIS RESOLVES (finding #2). This hook fires from the daemon's reap
    loop, and with RETAIN_DONE_RECENT=0 the on-disk row is either still
    'running'/exit_code=null (the daemon has not yet lock.save()'d the status
    flip) or already pruned (post-save) -- it is essentially NEVER observably
    'done' here. The old readers found the still-'running' row, hit the
    terminal-status guard, and returned None WITHOUT ever consulting the sidecar
    (the sidecar branch was only reached when the row was ABSENT), so a clean job
    kept stamping not_checked=verify-exit,launch-baseline. The .done.json sidecar
    is written from the still-complete job dict at fire time and is never pruned,
    so it is the only record that reliably certifies a terminal outcome here.
    Prefer it; consult the live row only when no sidecar exists yet, and only if
    it is itself terminal. A non-terminal live row with no sidecar returns None so
    the caller ABSTAINS (never a false PASS)."""
    rec = _completion_record(job_id)
    if isinstance(rec, dict):
        return rec
    j = _live_job(job_id)
    if isinstance(j, dict) and j.get("status") in ("done", "failed"):
        return j
    return None


def _job_field(job_id: str, key: str):
    """One field off the job record, LIVE row first then the durable sidecar.

    _notify_non_pass reads launched_by / launched_by_session through here, and
    the queue prunes the live row the instant a job finishes (finding #5), so
    without the sidecar fallback every done job's addressee would be unreadable.
    Fail-open, like launch_baseline()."""
    j = _live_job(job_id)
    if isinstance(j, dict) and j.get(key) is not None:
        return j.get(key)
    rec = _completion_record(job_id)
    if isinstance(rec, dict):
        return rec.get(key)
    return None


def _notify_non_pass(job_id: str, payload: dict, gate_json) -> None:
    """Make a non-pass verdict IMPOSSIBLE TO MISS. Never raises.

    THE GAP THIS CLOSES. This tool wrote gate.json, merged the handoff INDEX,
    printed one line, and stopped -- ZERO outbound contact of any kind. A `fail`
    with code_high=1 was byte-for-byte as quiet as a clean pass, so a real
    high-severity finding (radarr e3ccbf07fffa: _radarr_last_imported_source_title
    read only events[0].sourceTitle instead of iterating like its sibling)
    survived a human both-ways review and was caught ONLY because someone
    happened to open the JSON by hand.

    TWO CHANNELS, because the addressee is usually unknown:

      1. LOCAL, always -- stderr plus an append-only NON-PASS-GATES.md beside the
         gate records. This is the one that matters today: every job enqueued
         before ollama-queue.py began stamping launched_by has no addressee, so a
         notifier handling only the addressed case would stay silent for exactly
         the backlog that motivated it.

      2. The LAUNCHING SESSION, when one was stamped -- addressed by FILE, not by
         speaking a protocol. The obvious implementation is to connect to the
         launched_by socket and write a message, but the cc-socks wire format is
         not documented to this tool, and a wrong guess inside a try/except fails
         SILENTLY: the gate would look wired while notifying nobody. That is the
         exact "reads as working when it isn't" failure this gate exists to
         prevent, so it does not ship on a guess. A per-launcher inbox file is
         decidable instead -- the file is either there or it is not.

    STALENESS. launched_by is pid-keyed (/tmp/cc-socks/<pid>.sock), so a session
    can end and the OS can hand its pid, and its socket path, to an unrelated
    session. The inbox is named for that pid and the entry carries
    launched_by_session, so a stale delivery is self-evident to whoever reads it
    rather than looking like their own job's finding.

    Rule 2 throughout: advisory, and never able to change the gate's exit code.
    """
    v = payload.get("verdict", "?")
    counts = payload.get("counts") or {}
    top = ""
    for i in (payload.get("issues") or []):
        if (i or {}).get("severity") == "high":
            top = " | " + str(i.get("source", "?")) + ": " + str(i.get("what", ""))[:120]
            break

    line = ("[gate] NON-PASS " + str(job_id) + " verdict=" + str(v)
            + " code_high=" + str(counts.get("code_high", 0))
            + " code=" + str(counts.get("code", 0))
            + " input=" + str(counts.get("input", 0)) + top)

    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:
        pass

    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    row = ("- `" + str(job_id) + "` **" + str(v) + "** code_high="
           + str(counts.get("code_high", 0)) + " - " + stamp
           + " - `" + str(gate_json) + "`" + top + "\n")

    try:
        with (Path(gate_json).parent / "NON-PASS-GATES.md").open("a") as fh:
            fh.write(row)
    except Exception:
        pass

    sock = _job_field(job_id, "launched_by")
    if not sock:
        return
    sess = _job_field(job_id, "launched_by_session")
    stem = Path(str(sock).replace("uds:", "", 1)).stem
    srow = row.rstrip("\n") + (" (for session " + str(sess) + ")" if sess else "") + "\n"
    try:
        with (Path(gate_json).parent / ("NON-PASS-for-" + stem + ".md")).open("a") as fh:
            fh.write(srow)
    except Exception:
        pass


def launch_baseline(job_id: str) -> dict | None:
    """The tree state the queue stamped when it LAUNCHED this job's worker.

    Shape (set by ollama-queue.py at launch, before the model touches anything):
        job["launch_baseline"] = {"head": <sha>, "dirty": <porcelain line count>}

    WHY THE GATE CANNOT DERIVE THIS ITSELF. The gate runs post-hoc, so `git diff
    HEAD` swallows the model's work and any pre-existing edits indistinguishably.
    dashboard-newjobs-fix (2026-08-31) ran on a scratch dir still holding edits
    from an earlier PAUSED run of the same task: its verify passed AT BASELINE,
    the accept-on-verify-pass net took that exit 0 at face value, and the stored
    diff was contaminated with work this job never did. Nothing downstream could
    have caught it -- only the launcher knows what the tree looked like before.

    READ, NEVER WRITTEN, and fail-open on everything: a missing key means a job
    enqueued before the stamp landed, and the state file is written concurrently
    by the daemon, so a torn read must abstain rather than throw. An exception
    here must never break a dispatch (rule 2).
    """
    # Sidecar-first (by fire time the live row is usually pre-save 'running' or
    # already pruned -- see _terminal_facts), then the live row: launch_baseline
    # is stamped at LAUNCH, so it is valid even while the job is still running.
    rec = _terminal_facts(job_id) or _live_job(job_id)
    if isinstance(rec, dict):
        lb = rec.get("launch_baseline")
        return lb if isinstance(lb, dict) else None
    return None


def baseline_at(job_id: str) -> str | None:
    """WHEN the queue took this job's launch_baseline reading: 'launch', 'enqueue',
    or None for a job that predates the field.

    Why the gate has to know (2026-09-19). launch_baseline() above documents the
    stamp as "set by ollama-queue.py at launch, before the model touches anything"
    -- that was the agreed contract, and the gate has always reasoned as if it held.
    It did not: the queue stamped at ENQUEUE. For any job that waits before it runs
    -- i.e. every chained stage -- that measures the wrong moment. rt-costco s1 had
    two jobs enqueued ONE SECOND apart both recording dirty=0 on the same head, then
    running sequentially in the same worktree; the second launched into the tree the
    first had just dirtied while carrying a stamp saying it was clean, and this gate
    faithfully read dirty == 0 and cleared it.

    ollama-queue.py now re-measures immediately before spawning the worker and says
    which it did. A `dirty > 0` reading is real evidence whenever it was taken, but a
    CLEAN one only means anything if it was taken at launch -- so this is what lets
    apply_baseline() tell "clean" from "clean, measured too early to know".

    READ, NEVER WRITTEN, fail-open like launch_baseline()."""
    rec = _terminal_facts(job_id) or _live_job(job_id)
    if isinstance(rec, dict):
        at = rec.get("baseline_at")
        return at if at in ("launch", "enqueue") else None
    return None


def job_verify_exit(job_id: str) -> int | None:
    """The exit code of the job's OWN verify, off the queue's job record.

    STATUS-GUARDED. exit_code is stamped per attempt and a job in 'running'
    can still be carrying a stale nonzero from a previous one -- one such row
    exists in the live state file right now. Trusting it unguarded would invent
    a FAIL on a job that has not finished, and a gate that cries wolf inverts
    its own purpose. Only 'done' and 'failed' are terminal enough to read.

    Fail-open on everything else, exactly like launch_baseline().
    """
    # SIDECAR-FIRST terminal facts (finding #2): the live row is pre-save
    # 'running' or already pruned by fire time, so trusting it would abstain on
    # every clean job. _terminal_facts returns the durable sidecar when present,
    # else the live row only if it is itself terminal, else None (abstain).
    rec = _terminal_facts(job_id)
    if rec is None:
        return None
    if rec.get("status") not in ("done", "failed"):
        return None
    ec = rec.get("exit_code")
    return ec if isinstance(ec, int) else None


def job_facts(job_id: str) -> dict:
    """Durable copy of the job-record fields the handoff view needs later."""
    # Sidecar-first terminal facts, then the live row for the informational
    # case (a job still genuinely running with no sidecar yet). verify_failed_at_
    # baseline is the load-bearing condition for autonomous sign-off -- a verify
    # already green at baseline proves nothing about the diff -- and the queue
    # prunes finished rows, so only the sidecar can recover it later.
    rec = _terminal_facts(job_id) or _live_job(job_id)
    if not isinstance(rec, dict):
        return {}
    return {"job_status": rec.get("status"), "job_label": rec.get("label"),
            "job_model": rec.get("model"), "job_cwd": rec.get("cwd"),
            "job_host": rec.get("host_pref") or rec.get("host"),
            "job_verify": rec.get("verify"),
            "job_exit_code": rec.get("exit_code"),
            "verify_failed_at_baseline": rec.get("verify_failed_at_baseline"),
            # Honest enqueue-time preflight reading. Frozen here so the handoff
            # panel can show it after the queue prunes the live row, instead of
            # the review's ambiguous "not-run (enqueued separately)".
            "job_preflight": rec.get("preflight"),
            "scored_arm": rec.get("scored_arm")}


def job_is_ungateable(rec) -> bool:
    """PURE. True when this job record has NO code diff for the CODE gate to review.

    Mirrors ollama-queue._is_ungateable_job -- kept here as defence in depth so a
    hand/legacy invocation of this hook on a research or --runner job is also a
    no-op, not just the daemon path.

      * task_kind == "research"  -- a measurement/answer job (bake-off arm, probe,
        investigation): its deliverable is a CSV row or an answer, not a patch.
      * runner set               -- it ran an alternate --runner executable (the
        bake-off driver, a render) instead of ollama-worker.py on a sealed
        worktree, so nothing in the tree is an attributable dispatch diff.

    operator 2026-09-18: the bonsai ternary bake-off (task_kind=research, --runner
    bakeoff-runner.py) was scored verdict=fail purely from the runner's exit code,
    escalated to an authoritative re-gate, and that regate seized the exclusive
    Studio gate lane -- holding ~10 unrelated qwen auto-author jobs behind a gate
    that had nothing to review. gate-/regate- labels never reach this check: main()
    routes them to merge_review first.
    """
    if not isinstance(rec, dict):
        return False
    if str(rec.get("task_kind") or "") == "research":
        return True
    if rec.get("runner"):
        return True
    return False


def harness_residue_paths(cwd) -> list:
    """TRACKED product files currently modified that are EXACTLY what the tree's own
    `refimpl.py` produces from their HEAD text -- i.e. the pipeline's harness-check output
    (refimpl applied by hand by the authoring model, or left by a killed self-check), not
    anyone's work. [] unless EVERY currently-modified tracked non-harness file is explained
    (one unexplained path means real work is mixed in: explain nothing). refimpl runs in a
    scratch dir holding only the HEAD copies of those files, so it can neither touch the
    worktree nor succeed on a path it was not given. Fail-closed ([]) on any error."""
    import tempfile
    cwd = Path(cwd)
    try:
        ref = cwd / "refimpl.py"
        if not ref.is_file():
            return []
        d = subprocess.run(["git", "-C", str(cwd), "diff", "--name-only", "HEAD"],
                           capture_output=True, text=True, timeout=60)
        if d.returncode != 0:
            return []
        keep = {"task.md", "auto-task.md", "verify.sh", "refimpl.py", "check_literals.py",
                "auto-harness-check.py", ".dispatch-harness.json", "dispatch-env.ts",
                "dispatch_env.py", ".preflight-state.json", "test_fixture.py"}
        files = [f.strip() for f in d.stdout.splitlines()
                 if f.strip() and Path(f.strip()).name.lower() not in keep
                 and not _VERIFY_TEST_RE.match(Path(f.strip()).name)
                 and not _VERIFY_FILE_RE.match(Path(f.strip()).name)]
        if not files:
            return []
        with tempfile.TemporaryDirectory(prefix="gate-residue-") as td:
            tdp = Path(td)
            for f in files:
                g = subprocess.run(["git", "-C", str(cwd), "show", f"HEAD:{f}"],
                                   capture_output=True, timeout=60)
                if g.returncode != 0:
                    return []
                (tdp / f).parent.mkdir(parents=True, exist_ok=True)
                (tdp / f).write_bytes(g.stdout)
            r = subprocess.run([sys.executable, str(ref), str(tdp)], cwd=str(tdp),
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                return []
            for f in files:
                if (tdp / f).read_bytes() != (cwd / f).read_bytes():
                    return []
        return files
    except Exception:
        return []


def effective_launch_baseline(lb: dict | None, cwd, job_label: str, override: int = -1):
    """The launch baseline the gate should JUDGE. A HARNESS-AUTHORING job (auto-author-*)
    that launched `dirty` only because the previous stage left the reference solution
    applied to the tracked target is not "unattributable": the dirt is the pipeline's own
    harness-check output and the job's deliverable is the harness STATE, not a product
    diff (b90e77ace2b2 / rt-costco-receipt-attach-s2: stage-2's model ran `python3
    refimpl.py` by hand, stage 3 launched dirty=1, the gate called it UNTRUSTED and
    escalated an authoritative regate that froze other bundles). Only when the tree STILL
    shows that residue (harness_residue_paths) and it accounts for the whole count; the
    original is kept under dirty_raw. Everything else -- coding jobs, real dirt, an
    explicit --launch-dirty override -- is returned untouched."""
    try:
        if override >= 0 or not isinstance(lb, dict) or not _is_authoring_label(job_label):
            return lb
        raw = lb.get("dirty")
        if not isinstance(raw, int) or raw <= 0:
            return lb
        res = harness_residue_paths(cwd)
        if res and raw <= len(res):
            out = dict(lb)
            out["dirty"] = 0
            out["dirty_raw"] = raw
            out["dirty_explained_by"] = "harness-check residue: " + ", ".join(res[:4])
            return out
    except Exception:
        pass
    return lb


def apply_baseline(payload: dict, lb: dict | None, override: int,
                   measured_at: str | None = None) -> None:
    """Flag a dispatch that STARTED from a dirty tree. Decidable, no model.

    HIGH and category=input: the diff is not attributable to this job, and the
    verify's exit 0 may predate the model entirely. It is deliberately NOT a
    'fail' -- gate.py's standing rule is that only a CODE high fails, because an
    input problem must never condemn code that may well be correct. But it must
    also never render as a clean pass, so any pass form is demoted to concerns.
    That is a different claim from the usual input finding: not 'the task was
    imperfect' but 'this result is unattributable'.
    """
    dirty = override if override >= 0 else (lb or {}).get("dirty")
    if not isinstance(dirty, int):
        # No stamp = job predates it, or a torn read. UNKNOWN, and unknown is
        # not clean -- recorded so the abstain is visible, same rule as
        # not_checked. Never invents a flag from absence.
        payload.setdefault("not_checked", []).append(
            "launch-baseline (queue recorded no launch_baseline for this job; "
            "a dirty starting tree could not be ruled out)")
        return
    payload["launch_baseline"] = dict(lb or {"dirty": dirty})
    if measured_at:
        payload["baseline_at"] = measured_at
    if dirty <= 0:
        # A CLEAN reading is only worth anything if it was taken AT LAUNCH. Taken
        # at enqueue it says the tree was clean when the job joined the queue,
        # which tells us nothing about the tree it actually started from -- and
        # that gap is exactly how a chained refine stage inherited the previous
        # stage's uncommitted work while reading as pristine. Same rule as the
        # missing-stamp branch above: unknown is not clean, and the abstain is
        # recorded rather than silently passing.
        #
        # An explicit --launch-dirty override is exempt: the operator measured it
        # themselves and their count stands whatever the queue recorded.
        if override < 0 and measured_at != "launch":
            payload.setdefault("not_checked", []).append(
                "launch-baseline (the clean reading was taken at "
                f"{measured_at or 'an unrecorded time'}, not at launch; a tree "
                "dirtied between enqueue and launch would not show here)")
        return
    payload.setdefault("issues", []).append(
        {"severity": "high", "file": "", "line": 0,
         "what": f"the dispatch STARTED from a dirty tree ({dirty} uncommitted "
                 f"path(s) at launch); the diff is not attributable to this job "
                 f"and verify's exit 0 may predate the model's work",
         "source": "launch-baseline", "category": "input"})
    payload["untrusted"] = [f"launch baseline was dirty ({dirty} path(s))"]
    c = payload.setdefault("counts", {"code_high": 0, "code": 0, "input": 0, "total": 0})
    c["input"] = c.get("input", 0) + 1
    c["total"] = c.get("total", 0) + 1
    if str(payload.get("verdict", "")).startswith("pass"):
        payload["verdict"] = "concerns"


def autofix_chain_base(job_id: str, cwd, state_path=None) -> str:
    """The ROOT job's launch-baseline commit for a gate auto-fix round, else "".

    An auto-fix round (auto_fix_round > 0) runs on a tree whose HEAD is a queue
    "seal round baseline" commit holding the PREVIOUS attempt, so `git diff HEAD`
    showed only this round's delta. rt-egift-link-s1-s0 r1 (01fdda651fba) fixed a
    misspelled name: its whole diff was a rename, the reviewer read it as "CREATE
    TABLE changed to CREATE UNIQUE INDEX", and relevance mutated only a constraint
    NAME (an equivalent mutant) -> CONCERNS/LOW on a correct, fully tested fix. The
    deliverable is cumulative, so judge it against the chain root's baseline --
    only when that commit is an ancestor of HEAD (else "" = old behaviour)."""
    try:
        qs = json.loads(Path(state_path or (STATE_DIR / "ollama-queue-state.json")).read_text())
        jobs = qs.get("jobs") or []
        if isinstance(jobs, dict):
            jobs = list(jobs.values())
        me = next((j for j in jobs if j.get("id") == job_id), None)
        if not me or int(me.get("auto_fix_round") or 0) <= 0:
            return ""
        root = str(me.get("auto_fix_root") or "")
        if not root or root == job_id:
            return ""
        rj = next((j for j in jobs if j.get("id") == root), None)
        head = str(((rj or {}).get("launch_baseline") or {}).get("head") or "")
        if not head:
            try:
                d = json.loads((STATE_DIR / "ollama-queue-logs" / f"{root}.gate.json").read_text())
                head = str((d.get("launch_baseline") or {}).get("head") or "")
            except Exception:
                head = ""
        if not head:
            return ""
        r = subprocess.run(["git", "-C", str(cwd), "merge-base", "--is-ancestor", head, "HEAD"],
                           capture_output=True, text=True, timeout=30)
        return head if r.returncode == 0 else ""
    except Exception:
        return ""


def get_diff(cwd: Path, out: Path, scaffold: dict | None = None,
             excluded: list | None = None, base: str = "") -> tuple[Path | None, str]:
    """(diff_path, how). Git-backed cwd is the normal case; anything else skips.

    scaffold: {relpath: sha256} pinned by the preflight. An UNTRACKED file whose
    bytes still match its pinned digest is the scaffold, not the model's work,
    and is left out of the diff (names appended to `excluded`). Without this,
    test_fixture.py rode into every diff as a "new file", the change class was
    never bounded_single_site, and autonomous sign-off could not fire on any
    dispatch that shipped a fixture -- which is every dispatch the preflight
    accepts. A scaffold file the MODEL edited no longer matches its digest, so
    it stays in the diff and scope-check sees it."""
    # Detect the repo with `git rev-parse`, not `(cwd/".git").exists()`. The old
    # check skipped the gate on two legitimate cases it should have reviewed:
    # (1) cwd is a SUBDIRECTORY of the repo -- `.git` is at the repo root, not in
    #     cwd (e.g. a dispatch run in <worktree>/sidecar); and
    # (2) cwd is a git WORKTREE, whose `.git` is a *file* (`gitdir: ...`) pointing
    #     at the parent's worktrees/ dir -- the whole point of worktree dispatches.
    # rev-parse walks up and resolves both. The `git diff`/`ls-files` calls below
    # already pass `-C cwd` and git handles the subdir/worktree cases itself.
    try:
        chk = subprocess.run(["git", "-C", str(cwd), "rev-parse",
                              "--is-inside-work-tree"],
                             capture_output=True, text=True, timeout=30)
    except Exception as e:
        return None, f"git rev-parse failed: {type(e).__name__}"
    if chk.returncode != 0 or chk.stdout.strip() != "true":
        return None, ("cwd is not inside a git work tree, so there is no baseline "
                      "to diff against -- nothing to review")
    try:
        r = subprocess.run(["git", "-C", str(cwd), "diff", base or "HEAD"],
                           capture_output=True, text=True, timeout=120)
    except Exception as e:
        return None, f"git diff failed: {type(e).__name__}"
    text = r.stdout or ""
    if not text.strip():
        # Also try staged/untracked-aware form before concluding no change.
        r2 = subprocess.run(["git", "-C", str(cwd), "diff"],
                            capture_output=True, text=True, timeout=120)
        text = r2.stdout or ""
    # UNTRACKED FILES ARE PART OF THE WORK. `git diff HEAD` cannot see them, so a
    # dispatch whose deliverable is a NEW file showed the gate an incomplete
    # diff -- and every check downstream then judged work it could not see.
    # signoff-stage1 (24f5cd44262e) delivered a new signoff.py: completeness
    # reported two of its declared literals "absent from the diff" when both
    # were implemented, and the review model reviewed two support files while
    # the actual deliverable was invisible.
    #
    # Built with `git diff --no-index`, which is READ-ONLY. The obvious
    # alternative, `git add -N`, mutates the index of a tree we do not own.
    extra, added = [], 0
    _authored = _manifest_authored(cwd)
    try:
        u = subprocess.run(["git", "-C", str(cwd), "ls-files", "--others",
                            "--exclude-standard"],
                           capture_output=True, text=True, timeout=60)
        for rel in (u.stdout or "").splitlines():
            rel = rel.strip()
            if not rel or added >= 50:
                continue
            # DISPATCH SCAFFOLDING IS NOT MODEL OUTPUT. In a scratch dir where
            # nothing is committed, the task file and the verify are untracked
            # too -- so including them made every such job report "TASK.md
            # edited but never named by the task" and, worse, tripped the
            # self-test finding on a verify.sh the DISPATCHER wrote. Excluded
            # only on the UNTRACKED path: a model editing a TRACKED verify.sh
            # still shows up in `git diff HEAD` and must still be flagged.
            # The verify HARNESS is the one thing a review must never review:
            # the model is told not to touch it (TASK.md ## Scope) and doesn't,
            # so it rides in as an untracked "new file" and the reviewer speaks
            # to code that is not the model's work. verify_impl.mjs leaking in is
            # exactly how a review was steered onto a scaffold file. Excluded
            # only on the UNTRACKED path, symmetric with verify.sh: a model
            # editing a TRACKED harness still shows in `git diff HEAD` and counts.
            _name = Path(rel).name.lower()
            # Bug #7 (2026-09-18): use the CANONICAL scaffold-basename set, not a
            # shorter hand-copied list. The old inline set omitted check_literals.py,
            # test_fixture.py, AUTO-TASK.md, auto-harness-check.py and
            # .dispatch-harness.json, so on an auto-author job (where the model is
            # SUPPOSED to rewrite the harness, so the digest match at :1049 never fires
            # and there are no pinned digests on a first author) all five leaked into
            # the diff -- aw-airport-groups came back "6 changed files" for a one-file
            # slice, 5 of them scaffold. _SCAFFOLD_BASENAMES is the single source of
            # truth shared with the apply step; union it with the legacy extras below.
            _scaffold_lc = {n.lower() for n in _SCAFFOLD_BASENAMES}
            if (_name in _scaffold_lc
                    or rel in _authored
                    or _name in {"task.md", "task.json", "verify.sh", "verify_impl.mjs",
                          "verify_impl.js", "task.txt", "run.json", "refimpl.py",
                          ".preflight-state.json"}
                    # verify[-_]impl.{mjs,cjs,mts,cts,js,ts,py,sh}: the hyphen
                    # and .mts/.cts forms cover the hand-rolled TS harness
                    # (verify-impl.mts) a name-exact list let ride into the diff
                    # as a "new file", which is how scope-check flagged "the
                    # diff edits the VERIFY" on a job that never touched it.
                    or re.match(r"^verify[-_]?(impl)?\.(mjs|cjs|mts|cts|js|ts|py|sh)$", _name)
                    # verify.test.{ext}: the node-test scaffold's starter test
                    # file (ollama-dispatch-scaffold --ts-runner node-test). It is
                    # authored harness, not model output; same untracked-only leak
                    # as verify_impl above if the operator did not --seal-baseline.
                    or re.match(r"^verify\.test\.(mjs|cjs|mts|cts|js|ts)$", _name)):
                if excluded is not None:
                    excluded.append(rel)
                continue
            f = cwd / rel
            if scaffold and rel in scaffold and _sha256(f) == scaffold[rel]:
                if excluded is not None:
                    excluded.append(rel)
                continue
            try:
                if not f.is_file() or f.stat().st_size > 512_000:
                    continue
                # Skip binaries by looking for a NUL byte, NOT by trying to
                # decode: a NUL is valid UTF-8, so read_text() succeeds on a
                # binary file and the "decode error means binary" test never
                # fires. Caught by asserting the property on a real binary
                # rather than trusting the idiom.
                if b"\x00" in f.read_bytes()[:8192]:
                    continue
            except Exception:
                continue
            d = subprocess.run(["git", "-C", str(cwd), "diff", "--no-index",
                                "--", "/dev/null", rel],
                               capture_output=True, text=True, timeout=60)
            if d.stdout:
                extra.append(d.stdout); added += 1
    except Exception:
        pass
    if extra:
        text = (text + "\n" + "\n".join(extra)) if text.strip() else "\n".join(extra)

    if not text.strip():
        return None, "no changes to review (git diff is empty)"
    out.write_text(text)
    return out, ("git diff HEAD" + (f" + {added} untracked file(s)" if added else ""))


def get_diff_locked(cwd, out, scaffold=None, excluded=None, base: str = ""):
    """get_diff() taken under the worktree's tree lock (see the TREE RACE note at the
    call site). A lock held past TREE_LOCK_WAIT_S is not fatal: read the diff anyway."""
    _dlk = _tree_lock_acquire(cwd, timeout=TREE_LOCK_WAIT_S)
    try:
        return get_diff(cwd, out, scaffold, excluded, base=base)
    finally:
        _tree_lock_release(_dlk)


RELEVANCE_MAX_MUTANTS = int(os.environ.get("GATE_RELEVANCE_MAX_MUTANTS") or 40)
RELEVANCE_BUDGET_S = int(os.environ.get("GATE_RELEVANCE_BUDGET_S") or 300)
_SURVIVOR_KEYS = ("file", "line", "class", "mutation", "snippet")


def relevance_verify_cmd(job_label: str, verify: str | None) -> str | None:
    """PURE. What --verify should verify-relevance.py actually mutate-test against.

    For an authoring/refine job, `verify` is the harness SELF-check
    (`python3 auto-harness-check.py`), not a discriminator over the target file:
    that script resets its declared target to HEAD before doing anything, then
    runs its OWN internal baseline/refimpl/verify/revert cycle. Mutating the
    on-disk target and asking IT whether the mutation survives is a category
    error -- it wipes the mutation via the reset before ever measuring, then
    certifies itself blind (proven directly: it prints VERIFY_OK even on a
    clean, completely-unfixed baseline). Every mutant "survives" by
    construction, producing a spurious LOW/0.0 relevance verdict regardless of
    actual fixture quality (job 5843edc8a589) and contributing to the
    "no tracked diff" verdict on 1c94b432dcaa -- both escalated on exactly this
    2026-09-26. Substitute `bash verify.sh` (the authored fixture itself) in
    that one case -- the same substitution advance_to_coding() already makes
    when sealing the fixture for the real coding job. Anything else (a real
    coding job's own bash verify.sh, or a non-authoring label) passes through
    unchanged."""
    if (job_label.startswith(("auto-author-", "auto-refine-"))
            and verify and "auto-harness-check.py" in verify):
        return "bash verify.sh"
    return verify


def _locked_test_target_fixture(cwd) -> bool:
    """True only when cwd/test_fixture.py MATCHES (modulo the draft marker + its confirm residue) ollama-dispatch-auto's
    locked runner for the manifest's TEST-FILE target (mirrors the preflight's
    locked_test_target_fixture). Fail-closed: any error -> False."""
    return False    # the old auto tool that owned the locked runner is retired


TREE_LOCK_NAME = "dispatch-tree.lock"     # git-dir tree lock shared with dispatch drivers
TREE_LOCK_WAIT_S = int(os.environ.get("GATE_TREE_LOCK_WAIT_S") or 1200)


def _tree_lock_acquire(cwd, timeout=TREE_LOCK_WAIT_S):
    """Exclusive flock on <git-dir of cwd>/dispatch-tree.lock, polled until
    `timeout`. Returns the open handle (release with _tree_lock_release), None when
    the tree has no git dir (nothing to coordinate with -- proceed), or False when
    the lock stayed held past `timeout`. Never raises."""
    import fcntl
    try:
        p = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--absolute-git-dir"],
                           capture_output=True, text=True, timeout=15)
        if p.returncode != 0 or not p.stdout.strip():
            return None
        fh = open(Path(p.stdout.strip()) / TREE_LOCK_NAME, "a+")
    except Exception:
        return None
    end = time.time() + max(0, timeout)
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if time.time() >= end:
                fh.close()
                return False
            time.sleep(1)


def _tree_lock_release(fh) -> None:
    if not fh:
        return
    try:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        fh.close()
    except Exception:
        pass


def _relevance_run(cmd, cwd, verify_cmd, task_file, base, bundle=None):
    """verify-relevance.py --applied on the finished tree. CPU lane (Phase 6): the Unraid runner when
    the stage can faithfully run there (cpu_stage.run_stage), else EXACTLY the old local subprocess.
    Returns an object with .returncode/.stdout/.stderr like subprocess.run. The caller holds the
    worktree tree lock around this; cpu_stage re-checks the HEAD+diff snapshot after the runner
    finishes and discards a stale result. A remote record that is not a verdict JSON is vetoed and the
    local run happens instead."""
    import shlex
    import types

    def _local():
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=RELEVANCE_BUDGET_S + 900)
        return r.returncode, r.stdout, r.stderr, False
    try:
        sys.path.insert(0, str(BIN)) if str(BIN) not in sys.path else None
        import cpu_stage as cs
    except Exception:
        rc, so, se, _ = _local()
        return types.SimpleNamespace(returncode=rc, stdout=so, stderr=se)
    tools = {}
    rcmd = ['python3 "$HOME/bin/verify-relevance.py" . --applied --verify ' + shlex.quote(verify_cmd)
            + " --json --max-mutants %d --budget-s %d" % (RELEVANCE_MAX_MUTANTS, RELEVANCE_BUDGET_S)]
    if task_file:
        tf = Path(task_file).expanduser().resolve()
        try:
            rel = tf.relative_to(Path(cwd).resolve())
            rcmd.append("--task-file " + shlex.quote(str(rel)))
        except ValueError:
            try:
                tools["bin/task-file"] = tf.read_bytes()
                rcmd.append('--task-file "$HOME/bin/task-file"')
            except OSError:
                rc, so, se, _ = _local()          # unreadable task file: let the local path report it
                return types.SimpleNamespace(returncode=rc, stdout=so, stderr=se)
    if base:
        rcmd.append("--base " + shlex.quote(base))

    def _check(res):
        t = (res.stdout or "").strip()
        if not t.startswith("{"):
            if res.exit_code == 3 and "nothing to mutate" in (res.stderr or "") and not res.timed_out:
                return None       # verify-relevance's own clean abstain: a local re-run would say the same
            return "not a JSON verdict (rc=%s)" % res.exit_code
        try:
            json.loads(t)
        except Exception:
            return "unparseable JSON"
        return None
    res = cs.run_stage(cwd, " ".join(rcmd), RELEVANCE_BUDGET_S + 900, "relevance",
                       bundle or cs.bundle_key(wt=cwd), _local, tools=tools,
                       env={"DISPATCH_VERIFY_SANDBOX": "1"}, check=_check, gzb64=True)
    if res.timed_out:
        raise subprocess.TimeoutExpired(cmd, RELEVANCE_BUDGET_S + 900)
    return types.SimpleNamespace(returncode=res.exit_code, stdout=res.stdout, stderr=res.stderr)


def measure_relevance(payload: dict, cwd: Path, verify_cmd: str,
                      task_file: str | None = None, base: str = "", bundle: str | None = None) -> None:
    """verify-relevance.py --applied on the job's finished tree -> payload["verify_relevance"].

    Three-valued and always present when it can be attempted: relevant / low /
    unproven, each with the reason and the named survivors. When it cannot be
    attempted the abstain goes to not_checked so the one-line print shows it.
    The tree is mutated IN PLACE one file at a time and restored after every
    mutant (verify-relevance's contract, asserted by its own test G). The job is
    finished, but its ORCHESTRATOR may not be (an auto-author round's
    continuation self-check runs in this same tree), so the mutation runs under
    the worktree's tree lock -- see _tree_lock_acquire.
    Advisory like everything here: never raises, never changes the exit code.
    """
    nc = payload.setdefault("not_checked", [])
    if not verify_cmd:
        nc.append("verify-relevance (the job declares no --verify, so there is "
                  "nothing to mutate against)")
        return
    if _locked_test_target_fixture(cwd):
        # TEST-FILE target (2026-10-03): same rule as the preflight -- the verify
        # is the target's own tests via auto's locked runner; mutating test code
        # is not a relevance measure (equivalent / unkillable survivors).
        payload["verify_relevance"] = {
            "verdict": "unproven", "score": None, "source": "gate-applied",
            "not_applicable": "test-file-target",
            "reason": ("N/A for a TEST-FILE target: the verify is the target's own "
                       "tests (locked runner fixture); mutating test code is not a "
                       "relevance measure -- human relevance read")}
        return
    if not RELEVANCE.is_file():
        nc.append(f"verify-relevance ({RELEVANCE} is missing)")
        return
    cmd = ["python3", str(RELEVANCE), str(cwd), "--applied",
           "--verify", verify_cmd, "--json",
           "--max-mutants", str(RELEVANCE_MAX_MUTANTS),
           "--budget-s", str(RELEVANCE_BUDGET_S)]
    if task_file:
        cmd += ["--task-file", str(Path(task_file).expanduser().resolve())]
    if base:
        cmd += ["--base", base]
    # NOT "nothing else runs in its worktree" (2026-10-05, rt-egift-link-s1-s0
    # d25ff267b7c9): for an auto-author/auto-refine round, ollama-dispatch-auto is
    # still driving this tree and runs its continuation self-check / preflight the
    # moment the job turns terminal. Mutate only under the worktree's tree lock
    # (ollama-dispatch-auto.tree_lock: flock on <git-dir>/dispatch-tree.lock); a
    # lock we cannot get within the window means another driver is inside the tree
    # -- abstain rather than mutate under it.
    _lk = _tree_lock_acquire(cwd, timeout=TREE_LOCK_WAIT_S)
    if _lk is False:
        payload["verify_relevance"] = {
            "verdict": "unproven", "score": None, "source": "gate-applied",
            "reason": (f"verify-relevance NOT run: the worktree's tree lock stayed held "
                       f"by another driver for {TREE_LOCK_WAIT_S}s (an in-flight "
                       f"authoring self-check/preflight) -- mutating under it would "
                       f"corrupt both measurements")}
        return
    try:
        if bundle is None and payload.get("job_id"):
            bundle = _job_field(payload["job_id"], "bundle")
        r = _relevance_run(cmd, cwd, verify_cmd, task_file, base, bundle)
        rec = json.loads(r.stdout) if r.stdout.strip().startswith("{") else None
    except Exception as e:
        _tree_lock_release(_lk)
        payload["verify_relevance"] = {
            "verdict": "unproven", "score": None, "source": "gate-applied",
            "reason": f"verify-relevance did not run: {type(e).__name__}: {str(e)[:160]}"}
        return
    _tree_lock_release(_lk)
    if rec is None:
        payload["verify_relevance"] = {
            "verdict": "unproven", "score": None, "source": "gate-applied",
            "reason": ("verify-relevance exited " + str(r.returncode) + ": "
                       + (r.stderr or r.stdout or "").strip()[-200:])}
        return
    # Keep the record compact: the full mutant table stays on stdout of the
    # tool; the gate record carries the decision and what a human needs to
    # argue with it (survivors, unexercised sites, the two scores).
    block = {k: rec.get(k) for k in (
        "verdict", "score", "threshold", "mutant_score", "site_score",
        "site_coverage", "killed", "survived", "evidence_mutants", "generated",
        "literal_breaking", "crash_kills", "truncated", "untried_sites",
        "unexercised_sites", "reason", "seconds")}
    block["survivors"] = [{k: s.get(k) for k in _SURVIVOR_KEYS}
                          for s in (rec.get("survivors") or [])[:8]]
    # SOURCE-TEXT HARNESS (source_text_harness.py via verify-relevance): carried so
    # autofix_classify / the escalation text can name the real defect.
    if rec.get("source_text_harness"):
        block["source_text_harness"] = rec["source_text_harness"]
    # EQUIVALENT-MUTANT SWEEP outcome (instrument every filter): the fixed key list above hid
    # that mutant_sweep never ran on the CPU lane (import failed -> rec["sweep_error"]), so a
    # 'low' verdict inflated by equivalent survivors was indistinguishable from a real fixture
    # hole (cardBonuses-v2 964c4a833b82). Carry the sweep counters / error / note verbatim.
    for _k in ("sweep", "sweep_error", "sweep_note"):
        if rec.get(_k):
            block[_k] = rec[_k]
    if rec.get("equivalent_survivors"):
        block["equivalent_survivors"] = len(rec["equivalent_survivors"])
    # REJECTED OPT-OUTS (2026-10-06 gate audit): verify-relevance reports, as
    # rec["optout_rejected"], every `# relevance: ignore` marker it refused to
    # honour (no reason, or a behaviour-bearing site). The fixed key list above
    # dropped it, so the gate record -- and every decision reading it -- could not
    # see that the job's author tried to exempt sites from mutation. Carried
    # verbatim (capped) so mechanical_evidence_gaps can refuse to auto-advance.
    if rec.get("optout_rejected"):
        block["optout_rejected"] = list(rec["optout_rejected"])[:20]
    block["source"] = "gate-applied"
    block["verify"] = verify_cmd
    payload["verify_relevance"] = block


# ------------------------------------------------------------------------------
# HARNESS-REFINE (Phase 2 item 5, 2026-10-08). A relevance SURVIVOR with VERIFY_OK is a
# defect in the FIXTURE (it does not kill a plausible-wrong build), not in the model's code
# and not a reason to park a human. Route it back as a bounded harness refine; only when
# the budget (dispatch_harness_gates.HARNESS_REFINE_CAP) is spent does it fall through to
# the old escalation, now worded as a RE-SPEC. Counter is per run-label, persisted.
# ------------------------------------------------------------------------------
def _hg_mod():
    try:
        sp = importlib.util.spec_from_file_location("dispatch_harness_gates", BIN / "dispatch_harness_gates.py")
        m = importlib.util.module_from_spec(sp)
        sp.loader.exec_module(m)
        return m
    except Exception:
        return None


def _refine_home() -> Path:
    return Path(os.environ.get("OLLAMA_DISPATCH_HOME") or (Path.home() / ".ollama-dispatch")).expanduser()


def harness_refine_label(label) -> str:
    return re.sub(r"\s*\[auto-fix r\d+\]\s*$", "", str(label or "")).strip()


def harness_refine_apply(dec: dict, payload: dict, label, state_dir=None, launch=None) -> dict:
    """Convert a `grader-suspect-relevance` escalate into a bounded harness refine.

    Returns the (possibly new) decision. Below the cap: {class: harness-refine, action:
    harness-refine}, a request file with the survivors is written to
    <home>/harness-refine/<label>.json, the counter is bumped, the auto-decision is logged,
    and (live mode only, never under TEST_MODE / DISPATCH_VERIFY_SANDBOX) the recorded
    `ollama-dispatch-auto <argv> --resume-harness` driver is relaunched detached through
    `launch(cmd, cwd)`. At the cap: the escalate decision is kept, reworded as a RE-SPEC."""
    if dec.get("class") != "grader-suspect-relevance":
        return dec
    lbl = harness_refine_label(label)
    if not lbl:
        return dec
    hg = _hg_mod()
    if hg is None:
        return dec
    d = Path(state_dir) if state_dir else _refine_home() / "harness-refine"
    f = d / (re.sub(r"[^\w.-]", "_", lbl) + ".json")
    try:
        rec = json.loads(f.read_text())
    except (OSError, ValueError):
        rec = {}
    count = int(rec.get("count") or 0)
    action, n = hg.refine_budget(count)
    vr = payload.get("verify_relevance") or {}
    survivors = [{k: sv.get(k) for k in ("file", "line", "mutation", "class")}
                 for sv in (vr.get("survivors") or [])[:8] if isinstance(sv, dict)]
    if action != "refine":
        return dict(dec, reasons=[f"harness-refine budget SPENT ({count}/{hg.HARNESS_REFINE_CAP} refine "
                                  f"rounds still left surviving mutants) -- the SPEC is the problem: "
                                  f"re-slice/re-spec it rather than refining the fixture again."]
                    + list(dec.get("reasons") or [])[:2])
    rec = {"label": lbl, "count": n, "survivors": survivors,
           "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "history": (rec.get("history") or [])[-5:] + [{"n": n, "survivors": survivors}]}
    try:
        d.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(rec, indent=1))
    except OSError:
        return dec          # cannot persist the counter -> never loop unbounded; keep the park
    launched = "request written (old auto driver retired; use mini-swe)"
    try:
        hg.log_auto_decision("harness-refine", lbl, "refine",
                             f"round {n}/{hg.HARNESS_REFINE_CAP}: {len(survivors)} surviving mutant(s) with "
                             f"VERIFY_OK -> fixture refine, not a park; {launched}")
    except Exception:
        pass
    return {"class": "harness-refine", "action": "harness-refine", "anchor": None,
            "reasons": [f"relevance survivors with VERIFY_OK: harness refine round {n}/"
                        f"{hg.HARNESS_REFINE_CAP} requested ({launched}); survivors in {f}"]}


def autofix_classify(payload: dict) -> dict:
    """PURE. Map a TERMINAL gate verdict to an auto-fix action. Never enqueues,
    never mutates a tree, never reads the queue. Returns
    {class, action, anchor, reasons}; action in {requeue, escalate, none}.

    FAIL-CLOSED: any verdict/finding shape not positively recognised as a
    model-actionable, DECIDABLE failure -> escalate. The requeue set is
    deliberately narrow -- spending GPU on a re-run is only justified when the
    failure is (a) real (a machine check said so, not just a reviewer opinion)
    and (b) something the model editing its code can actually change.

    The three classes from the design:
      (b) verify-red  : the job's OWN sealed verify exited nonzero. Machine truth,
                        the strongest and most decidable signal -> REQUEUE.
      (a) scope       : a concerns verdict whose only findings are scope ones
                        (edited a file the task never named) -> REQUEUE with a
                        tighter scope reminder. Decidable and model-actionable.
      (c) undecidable : dirty/unattributable baseline, invariant-guard
                        removed+re-added-literal FP, task/verify-quality
                        problems, a reviewer-only code-high with no red verify
                        (the false-FAIL class), verdict=error/unknown -> ESCALATE.
    """
    v = str(payload.get("verdict", ""))
    if v in ("pass", "pass-pending-review", "skipped"):
        return {"class": "clean", "action": "none", "anchor": None,
                "reasons": [f"verdict={v}: nothing to fix"]}
    if v in ("error",):
        return {"class": "gate-error", "action": "escalate", "anchor": None,
                "reasons": [f"gate verdict={v}: cannot classify, human needed"]}
    issues = payload.get("issues") or []
    # UNATTRIBUTABLE result: the tree was dirty at launch, so the diff is not this
    # job's and a re-run in the SAME dirty tree reproduces the contamination. Never
    # requeue -- this needs a human to clean/re-seal the tree. (Also: a requeue
    # here is a prime infinite-loop seed, because the dirty state persists.)
    if payload.get("untrusted"):
        return {"class": "unattributable", "action": "escalate", "anchor": None,
                "reasons": ["untrusted baseline: " + "; ".join(payload["untrusted"])[:200]]}
    code = [i for i in issues if i.get("category") == "code"]
    code_high = [i for i in code if i.get("severity") == "high"]
    inputs = [i for i in issues if i.get("category") == "input"]
    verify_red = [i for i in code_high if i.get("source") == "verify-exit"]
    review_high = [i for i in code_high if i.get("source") == "review"]
    other_high = [i for i in code_high
                  if i.get("source") not in ("verify-exit", "review")]
    scope = [i for i in inputs if i.get("source") == "scope"]
    # (b) THE SEALED VERIFY IS RED. Decidable machine truth; the model can act on
    # the named failing check. Highest-confidence requeue.
    if verify_red:
        return {"class": "verify-red", "action": "requeue", "anchor": "verify-exit",
                "reasons": [str(verify_red[0].get("what", ""))[:200]]}
    # (c) reviewer-only code-high, no red verify: the FALSE-FAIL risk class. Only
    # requeue if explicitly trusted; default escalate.
    if review_high and not other_high:
        if AUTOFIX_TRUST_REVIEWER:
            return {"class": "reviewer-code-high", "action": "requeue", "anchor": "review",
                    "reasons": [str(review_high[0].get("what", ""))[:200]]}
        return {"class": "reviewer-code-high", "action": "escalate", "anchor": None,
                "reasons": ["reviewer flagged a code high with no red verify "
                            "(false-FAIL class); GATE_AUTOFIX_TRUST_REVIEWER off"]}
    # NO PRODUCT CHANGE: the job delivered nothing (scaffold-only or empty diff)
    # and, when its verify was green, the verify was green at baseline. A re-run
    # cannot fix a verify that passes without a change -- the harness/spec is the
    # suspect, so this is a human/Claude-gate decision, never a requeue.
    npc = [i for i in code_high if i.get("source") == "no-product-change"]
    if npc:
        return {"class": "no-product-change", "action": "escalate", "anchor": None,
                "reasons": [str(npc[0].get("what", ""))[:220]]}
    # A code-high from some OTHER decidable source (tilde-path, tw-class-check):
    # decidable and model-actionable -> requeue.
    if other_high:
        return {"class": "code-high", "action": "requeue", "anchor": "code",
                "reasons": [f"{other_high[0].get('source')}: "
                            + str(other_high[0].get("what", ""))[:180]]}
    # (a) SCOPE-ONLY concerns: the only findings are scope (out-of-scope edits).
    # Decidable, and a tighter "do not touch X" reminder is something the model
    # acts on. Requires NO invariant-guard / launch-baseline finding riding along
    # (those are undecidable and would make a requeue pointless).
    #
    # `not code` (NOT `not code_high`, 2026-09-17): a reviewer code finding BELOW
    # high (e.g. job 3a533bacd6d8: a MEDIUM review "the deleted line was the
    # login-confirmation signal") is category=code and slips past a `code_high`
    # guard, so a scope-requeue would fire while a real behavioural concern rides
    # along that (i) the scope reminder cannot address and (ii) is never surfaced
    # in the requeue guidance (which carries dec.reasons=scope + only HIGH issues).
    # That is exactly the reviewer-opinion-without-a-red-verify false-FAIL class
    # this harness escalates rather than auto-acts on. Any code-category finding
    # -> not scope-only -> escalate so a human adjudicates. verify_red/other_high/
    # review_high already returned above, so the only code findings that reach here
    # are non-high reviewer ones; excluding them keeps the requeue set narrow.
    if v == "concerns" and scope and not code and not [
            i for i in inputs if i.get("source") in ("invariant-guard", "launch-baseline")]:
        return {"class": "scope", "action": "requeue", "anchor": "scope",
                "reasons": [str(i.get("what", ""))[:160] for i in scope[:3]]}
    # RELEVANCE-driven diagnosis (Task D): the harness's OWN relevance gate is the
    # authority on whether the fixture pins the property. Two failure shapes, both
    # of which "SUSPECT THE GRADER before model incapacity": the owed repair is a
    # HARNESS refine (add adversarial cases), which the ollama-dispatch-auto
    # orchestrator owns INLINE during authoring -- by the time a job is TERMINAL
    # here that inline budget is already spent, so the gate path ESCALATES with the
    # concrete diagnosis rather than re-running a coding self-fix (whose guidance --
    # "edit only the target, never a fixture" -- is exactly backwards for a harness
    # refine). Only fires when relevance was actually MEASURED and no code finding
    # rides along (a real code finding already returned above / falls to undecidable).
    vr = payload.get("verify_relevance") or {}
    rel_verdict = str(vr.get("verdict") or "")
    rel_measured = bool(vr) and rel_verdict != ""
    survivors = vr.get("survivors") or []
    _sth = vr.get("source_text_harness") or {}
    if rel_verdict == "low" and _sth.get("verdict") == "source-text" and not code:
        # The fixture asserts on the target's SOURCE TEXT (rt-bfmr-link-sync-
        # feedback, 2026-10-05). Not a survivor list to "kill" -- that is how
        # the regexes got there -- but a harness RE-AUTHOR: execute the code.
        return {"class": "grader-suspect-source-text", "action": "escalate",
                "anchor": None,
                "reasons": [f"SOURCE-TEXT HARNESS: {str(_sth.get('reason') or '')[:300]} "
                            f"-- the owed repair is a harness RE-AUTHOR whose fixture "
                            f"EXECUTES the target (extract an exported pure function / "
                            f"drive the export with a mocked fetch), not a code re-run "
                            f"and not more regex cases."]}
    if rel_measured and survivors and not code:
        det = "; ".join(
            (f"{(s.get('file') or '?')}:{s.get('line', '?')} "
             f"{s.get('mutation') or s.get('class') or ''}").strip()
            for s in survivors[:4])
        return {"class": "grader-suspect-relevance", "action": "escalate",
                "anchor": None,
                "reasons": [f"relevance gate left {len(survivors)} surviving mutant(s): "
                            f"{det} -- the fixture does not KILL a plausible-wrong "
                            f"build (not adversarial enough). Suspect the grader/harness "
                            f"before model incapacity; the owed repair is a harness REFINE "
                            f"(add adversarial cases), not a code re-run."]}
    if rel_measured and rel_verdict in ("unproven", "undecidable") and not survivors \
            and not code:
        return {"class": "grader-suspect-unproven", "action": "escalate",
                "anchor": None,
                "reasons": [f"relevance {rel_verdict} with no surviving mutant to act on "
                            f"-- the harness could not MEASURE relevance (verify-relevance "
                            f"or spec defect). Suspect the grader before model incapacity "
                            f"(verdict={v})."]}
    # Everything else (invariant-guard removed+re-added literal, verify-quality,
    # completeness, low input/scope, unknown) -> a TASK/HARNESS or undecidable
    # problem a code re-run cannot fix. Escalate.
    srcs = sorted({str(i.get("source", "?")) for i in issues}) or ["(none)"]
    return {"class": "undecidable", "action": "escalate", "anchor": None,
            "reasons": [f"verdict={v}; findings from {','.join(srcs)} are not a "
                        f"decidable, model-actionable failure"]}


def autofix_round(job_id: str, payload: dict) -> int:
    """The auto-fix retry round of the job that just completed. DECIDABLE from two
    independent durable sources, taking the MAX so a torn read of one cannot RESET
    the counter (the infinite-loop seed): (1) the job field auto_fix_round, read
    live-then-sidecar via _job_field; (2) an `[auto-fix rN]` marker baked into the
    label. A genuine original dispatch has neither -> 0, which is correct. A
    requeue always carries both, so a torn field read still recovers N from the
    label. Never negative."""
    n = 0
    f = _job_field(job_id, "auto_fix_round")
    if isinstance(f, int) and f > n:
        n = f
    lbl = str(payload.get("job_label") or _job_field(job_id, "label") or "")
    m = re.search(r"\[auto-fix r(\d+)\]", lbl)
    if m:
        try:
            n = max(n, int(m.group(1)))
        except ValueError:
            pass
    return max(0, n)


def autofix_root(job_id: str, payload: dict) -> str:
    r = _job_field(job_id, "auto_fix_root")
    return str(r) if r else str(job_id)


# ==============================================================================
# END-TO-END AUTO-PIPELINE decision core (Task C). PURE functions: no queue reads,
# no subprocess, no tree mutation -- they map a TERMINAL gate payload to the
# success-path action, so --self-test exercises the EXACT logic the daemon runs.
# ==============================================================================
def _is_authoring_label(job_label) -> bool:
    """An ollama-dispatch-auto HARNESS-authoring job (authors/refines the fixture),
    as opposed to a real CODING dispatch."""
    return str(job_label or "").startswith(("auto-author-", "auto-refine-"))


def _is_plangen_label(job_label) -> bool:
    """One round of ollama-dispatch-plan (label `plan-gen-<label>-r<N>`), which owns
    its own round/refine loop."""
    return str(job_label or "").startswith("plan-gen-")


def is_investigation_task(task_text, verify, task_kind=None) -> bool:
    """PURE. True for a dispatch whose deliverable is a WRITTEN FINDING, not a code
    diff: task_kind=research, or a diagnosis/investigation. Same signals as
    ollama-queue._dispatch_is_investigation (DIAGNOSIS.md named in the task or the
    verify; or a diagnose-word AND a findings-word together) -- duplicated, not
    imported, because this hook runs standalone.

    Why (2026-10-02, plex-automation pa-grab-dedupe-diagnosis 37154c38fc6f): a
    diagnosis is enqueued task_kind=coding (research would demand web_fetch), so
    the only research guard here missed it; the gate scored its no-code diff as
    low-scope/verify-quality CONCERNS and auto-fix requeued it (4ed5b4908033),
    which thrashed and escalated -- a GPU slot spent "fixing" a report."""
    if str(task_kind or "") == "research":
        return True
    hay, v = task_text or "", verify or ""
    if "DIAGNOSIS.md" in hay or "DIAGNOSIS.md" in v:
        return True
    return bool(re.search(r"\bdiagnos(?:e|is|tic|tics|ing|ed)\b", hay, re.IGNORECASE)
                and re.search(r"\b(?:root cause|investigat\w*|report|findings?|"
                              r"write[- ]?up|analy[sz]e|analysis)\b", hay, re.IGNORECASE))


def _job_is_investigation(job_id: str, payload: dict) -> bool:
    """Fail-safe wrapper: reads the job's task file + verify. Any error -> False."""
    try:
        tf = _job_field(job_id, "task_file") or payload.get("task_file")
        text = ""
        if tf and Path(tf).is_file() and Path(tf).stat().st_size < 200_000:
            text = Path(tf).read_text(errors="replace")
        verify = (_job_field(job_id, "verify") or job_facts(job_id).get("job_verify")
                  or payload.get("job_verify") or "")
        return is_investigation_task(text, verify, _job_field(job_id, "task_kind"))
    except Exception:
        return False


def apply_policy(repo_name) -> str:
    """PURE. 'auto-apply' for a repo that redeploys from main after CI (the
    AUTO_DEPLOY_REPOS set), else 'ready' (a clean fix stops at 'ready to apply' for
    a human to land). Unknown/None repo -> 'ready' (fail-safe: never auto-deploy
    something we could not positively identify)."""
    return "auto-apply" if repo_name and str(repo_name) in AUTO_DEPLOY_REPOS else "ready"


def _relevance_proven(payload: dict) -> bool:
    """PURE. Did the relevance gate positively PROVE the fixture pins the property?
    verdict must be exactly 'relevant' AND there must be zero surviving mutants.
    Anything else -- 'unproven', 'undecidable', a missing block, any survivor -- is
    NOT proven, so the success path does not fire (the refine ladder / a human owns
    it). Fail-CLOSED: an unreadable/absent block reads as not-proven."""
    vr = payload.get("verify_relevance") or {}
    return str(vr.get("verdict")) == "relevant" and not (vr.get("survivors") or [])


def _gate_is_clean(payload: dict) -> bool:
    """PURE. A CLEAN terminal gate: verdict 'pass' with no code findings at all.
    'concerns'/'fail'/'error'/'pass-pending-review' are NOT clean -- pending-review
    is not terminal, and concerns/fail are the failure ladder's business. Any
    'untrusted' (dirty baseline) is never clean."""
    if str(payload.get("verdict")) != "pass":
        return False
    if payload.get("untrusted"):
        return False
    code = [i for i in (payload.get("issues") or []) if i.get("category") == "code"]
    return not code


def mechanical_evidence_gaps(payload: dict) -> list:
    """PURE. Why this CLEAN pass is NOT machine-proven -- the list of missing
    behavioural evidence. Empty list = every mechanical check a PASS can be argued
    from is present and positive.

    WHY (2026-10-06 gate audit): of 290 code PASS verdicts only 110 carried no
    mechanical weakness. 21 had no both-ways proof (the verify was never shown red
    at baseline -- it could be vacuous), 84 had relevance unproven, 53 relevant-
    with-survivors, and 216 were marked ready-to-apply anyway; a PASS rested on the
    reviewer model's prose alone. classify_advance's coding branch deliberately
    does not re-require relevance for a human-landed 'ready'; but the UNATTENDED
    'auto-apply' tier must not fire on a pass that a machine never proved. So
    every gap here downgrades auto-apply -> ready (a human lands it) and is recorded
    on the gate record as auto_pipeline_evidence_gaps. FAIL-CLOSED: an absent key
    counts as a gap."""
    gaps = []
    vr = payload.get("verify_relevance") or {}
    if payload.get("verify_failed_at_baseline") is not True:
        gaps.append("no both-ways proof: the verify was not shown to FAIL at baseline "
                    "(it could pass vacuously)")
    if not vr:
        gaps.append("relevance not measured (no verify_relevance block)")
    elif not _relevance_proven(payload):
        gaps.append(f"relevance not proven (verdict {vr.get('verdict')!r}, "
                    f"{len(vr.get('survivors') or [])} survivor(s))")
    if vr.get("truncated"):
        gaps.append("relevance measurement truncated (partial sample)")
    if vr.get("optout_rejected"):
        gaps.append(f"{len(vr['optout_rejected'])} relevance opt-out marker(s) were "
                    f"REJECTED (sites the author tried to exempt from mutation)")
    _sth = str((vr.get("source_text_harness") or {}).get("verdict") or "")
    if _sth in ("source-text", "mixed"):
        gaps.append(f"fixture asserts on SOURCE TEXT ({_sth}), not behaviour")
    rv = str(payload.get("review_verdict") or "").strip().upper()
    if rv != "PASS":
        gaps.append(f"reviewer verdict is {payload.get('review_verdict')!r}, not a "
                    f"plain PASS")
    if payload.get("second_opinion_disagreement"):
        gaps.append("the cross-family second opinion DISAGREES")
    if payload.get("untrusted"):
        gaps.append("untrusted: " + "; ".join(map(str, payload["untrusted"]))[:160])
    j = payload.get("judged") or {}
    if not (isinstance(j, dict) and j.get("files")):
        gaps.append("no judged-identity stamp naming the product files judged")
    return gaps


# --- RISK-TIERED auto-confirm (operator 2026-09-17) -------------------------------
# advance_to_coding() runs `ollama-dispatch-draft --confirm` programmatically --
# it seals a MODEL-AUTHORED fixture without a human ever reading it. That marker
# (DRAFT_UNCONFIRMED) exists because the one thing no gate can decide is whether
# the cases are RELEVANT, and an hour before this was written the human review it
# stands for caught a P&L-breaking bug the mutation gate had passed (a fixture
# that pattern-matched a Prisma filter shape instead of evaluating it).
#
# So the auto-confirm is not removed and it is not made blanket. It is TIERED:
# it fires only for shapes where a wrong answer is cheap and visible, and it
# ALWAYS stands down on anything touching money/P&L, database writes or
# destructive paths -- there the human confirm is the control, and no amount of
# green evidence substitutes for it.
#
# Fail-CLOSED everywhere: an unreadable task, a missing lint, an unparsable
# policy file all resolve to "human". The cost of a false "human" is that the operator
# reads a fixture; the cost of a false "low" is an unreviewed money bug.
AUTO_CONFIRM_HUMAN_PATTERNS = [
    # money / P&L
    r"\bp&l\b", r"profit", r"revenue", r"payout", r"payment", r"invoice",
    r"refund", r"price|pricing", r"cost\b", r"ledger", r"balance", r"fee\b",
    r"tax\b", r"charge", r"billing", r"cashback", r"\bgift ?card",
    # database writes / migrations / schema
    r"\bmigrat", r"schema\.prisma", r"\bcreate\s+table", r"\balter\s+table",
    r"prisma\.\w+\.(create|update|upsert|delete|deleteMany|updateMany)",
    r"\binsert\s+into\b", r"\bupdate\s+\w+\s+set\b",
    # destructive
    r"\bdrop\s+table\b", r"\bdelete\s+from\b", r"\btruncate\b", r"rm\s+-rf",
    r"deleteMany", r"remove_data", r"unlink\(", r"shutil\.rmtree",
    # access / secrets (same posture as signoff's security-adjacent list)
    r"auth", r"token", r"passw", r"secret", r"credential", r"keychain",
]


# A minimal FAITHFUL python fixture (loads the real target, asserts on what it
# returned). Module-level because --self-test uses it in two places and the two
# must be the same fixture, or the tier tests and the shadow tests are measuring
# different things.
_SELFTEST_FAITHFUL_FIXTURE = (
    'import importlib.util, sys\n'
    'spec = importlib.util.spec_from_file_location("target", "t.py")\n'
    'target = importlib.util.module_from_spec(spec)\n'
    'sys.modules["target"] = target\n'
    'spec.loader.exec_module(target)\n'
    'CASES = [("a", lambda: target.f(1), 2)]\n')


def _auto_confirm_risk(cwd, payload: dict, lint_mod=None) -> tuple:
    """Is this harness safe to seal WITHOUT a human reading the cases?

    Returns (tier, reasons) with tier "low" or "human". PURE apart from reading
    the worktree, so --self-test drives the exact function the daemon runs.

    "low" requires ALL of:
      * the task text and the fixture name no money/P&L, DB-write or destructive
        surface (AUTO_CONFIRM_HUMAN_PATTERNS, matched case-insensitively);
      * the faithful-harness lint is clean on every fixture -- the automated pass
        that hunts exactly the shapes mutation testing misses (proxy/shape-
        matching, vacuous-truth, stub-tests-the-harness) must run and come back
        with no blocking finding, BEFORE any auto-seal;
      * the tree is readable at all.
    """
    import re as _re
    reasons = []
    try:
        wt = Path(cwd)
        task = (wt / "TASK.md")
        text = task.read_text(errors="replace") if task.is_file() else ""
    except Exception as e:
        return "human", [f"worktree unreadable ({e}) -- failing closed"]
    if not text.strip():
        return "human", ["TASK.md missing or empty -- nothing to judge the risk "
                         "of, so a human reads the cases"]
    hay = text + "\n" + "\n".join(str(f) for f in sorted(wt.glob("*")))
    for pat in AUTO_CONFIRM_HUMAN_PATTERNS:
        try:
            m = _re.search(pat, hay, _re.I)
        except _re.error:
            reasons.append(f"risk pattern {pat!r} does not compile -- treating "
                           f"as a hit (fail closed)")
            continue
        if m:
            reasons.append(f"touches a human-confirm surface: {m.group(0)!r} "
                           f"(~ /{pat}/)")
    # The automated relevance pass is MANDATORY before an auto-seal, not an
    # optional extra: it is the thing that would have caught rt-pl-exclude.
    mod = lint_mod
    if mod is None:
        lint = BIN / "faithful-harness-lint.py"
        if not lint.exists():
            reasons.append("faithful-harness-lint.py not installed -- the "
                           "automated relevance pass cannot run, so a human does")
        else:
            try:
                spec = importlib.util.spec_from_file_location("_fh_lint", lint)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            except Exception as e:
                reasons.append(f"faithful-harness-lint unusable ({e}) -- "
                               f"failing closed to a human")
                mod = None
    if mod is not None:
        fixtures = [p for pat in ("*.test.ts", "test_*.py", "verify_impl.*",
                                  "verify.test.*", "*_test.py")
                    for p in Path(cwd).glob(pat)]
        if not fixtures:
            reasons.append("no fixture file found to lint -- the automated "
                           "relevance pass has nothing to certify")
        for f in fixtures:
            try:
                findings = mod.lint_file(f)
            except Exception as e:
                reasons.append(f"lint failed on {f.name} ({e}) -- failing closed")
                continue
            for x in findings:
                if x.level == mod.FAIL:
                    reasons.append(f"{f.name}: {x.rule} -- {x.message[:120]}")
    if reasons:
        return "human", reasons
    return "low", ["no money/P&L, DB-write or destructive surface in the task, "
                   "and the faithful-harness lint is clean"]


def classify_advance(payload: dict, job_label, repo_name=None) -> dict:
    """PURE. The SUCCESS-path end-to-end decision for a TERMINAL gate verdict --
    the mirror of classify_autofix (which owns every FAILURE). Returns
    {stage, action, policy, reasons}; action in:
      advance-to-coding : an authoring job with a CLEAN gate + PROVEN relevance ->
                          seal the fixture (draft --confirm) + enqueue the CODING job.
      apply             : a CODING job with a CLEAN terminal PASS -> land the fix
                          per `policy` ('auto-apply' | 'ready').
      none              : not a success-path transition (a failure -- classify_autofix
                          handles it; relevance unproven; verdict not clean; or an
                          authoring/coding job that simply is not yet advanceable).

    FAIL-CLOSED: only the two positively-recognised clean transitions ever return an
    action; everything else is 'none', so nothing advances or applies on an
    ambiguous or failing verdict. GUARDRAIL: neither action edits verify/gate/
    check/fixtures (advance only seals the authored fixture + enqueues; apply only
    copies TARGET code to main, never merges the branch)."""
    authoring = _is_authoring_label(job_label)
    clean = _gate_is_clean(payload)
    if authoring:
        if clean and _relevance_proven(payload):
            return {"stage": "author", "action": "advance-to-coding", "policy": None,
                    "reasons": ["gate PASS + relevance 'relevant' (0 survivors): "
                                "harness proven; seal fixture + enqueue coding job"]}
        return {"stage": "author", "action": "none", "policy": None,
                "reasons": ["not a clean+proven harness; the refine ladder or a "
                            "human owns this (success path does not fire)"]}
    # A real CODING job: a clean terminal PASS is landable. relevance is the
    # authoring job's concern, not the coding job's -- do NOT re-require it here.
    if clean:
        pol = apply_policy(repo_name)
        gaps = mechanical_evidence_gaps(payload)
        if pol == "auto-apply" and gaps:
            # Unattended landing needs MACHINE proof, not the reviewer's word
            # (2026-10-06 gate audit). Still landable -- by a human.
            return {"stage": "code", "action": "apply", "policy": "ready",
                    "evidence_gaps": gaps,
                    "reasons": [f"gate PASS (clean, terminal) but NOT machine-proven -- "
                                f"auto-apply downgraded to 'ready': " + "; ".join(gaps)[:400]]}
        return {"stage": "code", "action": "apply", "policy": pol,
                "evidence_gaps": gaps,
                "reasons": [f"gate PASS (clean, terminal): land per policy '{pol}'"]}
    return {"stage": "code", "action": "none", "policy": None,
            "reasons": ["coding job not a clean terminal PASS; nothing to apply"]}


def autofix_models_tried(job_id: str, payload: dict) -> list:
    """The models this auto-fix CHAIN has already burned, from the durable job
    field `models_tried` (live row then sidecar). A genuine original dispatch has
    none -> []. Returns a de-duplicated, order-preserving list of model strings.
    Threaded forward by every model-fallback requeue so the ladder never re-runs a
    rung and the escalation reason can list what was tried."""
    raw = _job_field(job_id, "models_tried")
    if isinstance(raw, str):
        raw = [s.strip() for s in raw.split(",") if s.strip()]
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for m in raw:
        m = str(m).strip()
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def autofix_history(root: str, models_tried=None) -> str:
    """PURE-ish (reads only the durable chain ledger). A compact, human-first
    summary of the WHOLE auto-fix chain -- every recorded round's finding count and
    the finding SOURCES, plus the models burned -- so an escalation record hands the
    draining Opus/operator the full repair JOURNEY, not just the final round (Task D:
    'escalate with full repair history'). Fail-open: an unreadable/empty ledger ->
    a models-only line ('(no prior rounds)')."""
    lines = []
    try:
        d = _autofix_ledger_read(root)
        rounds = d.get("rounds") if isinstance(d, dict) else None
        if isinstance(rounds, dict):
            def _k(x):
                try:
                    return int(x)
                except (TypeError, ValueError):
                    return 0
            for k in sorted(rounds, key=_k):
                r = rounds[k] or {}
                fp = r.get("fingerprint") or []
                srcs = sorted({str(f).split("|", 1)[0] for f in fp})[:4]
                lines.append(f"r{k}={r.get('total', '?')} finding(s)"
                             + (f" [{','.join(srcs)}]" if srcs else ""))
    except Exception:
        pass
    tried = ", ".join(str(m) for m in (models_tried or [])) or "(none)"
    hist = "; ".join(lines) if lines else "(no prior rounds)"
    return f"repair history: {hist}; models tried: {tried}"


# Map an autofix_classify() class to the needs_opus escalation category the queue
# understands (ESCALATION_CATEGORIES in ollama-queue.py). Capability exhaustion --
# the ladder ran out -- is a persistent-nogo; a real gate/tool error is rc2;
# everything else a weaker coordinator must not decide alone is `other`. (The
# ladder never fires for these judgment classes; they escalate directly.)
_ESCALATION_CATEGORY_BY_CLASS = {
    "ladder-exhausted": "persistent-nogo",
    # Early suspect-the-grader: the SAME finding-set from >=2 distinct models.
    # Real attempts were made (persistent-nogo), but the diagnosis "inspect the
    # fixture/spec, not model capability" is carried up front in the reason text.
    "grader-suspect": "persistent-nogo",
    "thrash": "persistent-nogo",
    "gate-error": "rc2",
    "unattributable": "other",
    "reviewer-code-high": "other",
    "undecidable": "other",
    # Task D: relevance/grader-suspicion. Surviving mutants / an unmeasurable
    # relevance are HARNESS-side defects -- routed to the relevance categories the
    # queue already understands so the Opus drain sees "suspect the grader", not a
    # generic capability park.
    "grader-suspect-relevance": "undecidable-relevance",
    "grader-suspect-unproven": "undecidable-unproven",
    # A source-text fixture is a relevance defect of the harness (an existing
    # category the queue understands -- no new category string for ollama-queue.py).
    "grader-suspect-source-text": "undecidable-relevance",
}


def autofix_escalation_category(cls: str) -> str:
    return _ESCALATION_CATEGORY_BY_CLASS.get(str(cls), "other")


def autofix_build_escalate(job_id: str, root: str, reason: str,
                           category: str, gate_json: Path) -> list:
    """Build the `ollama-queue.py escalate` argv that PARKS a job as needs_opus
    (Tier 2). Does NOT run it; it is the exact shadow preview and the live command.
    Reuses the existing escalate machinery -- reason lists the ladder history, the
    category is derived from the failure class, and the gate JSON is snapshotted
    onto the parked job for the draining Opus/operator pass."""
    return ["python3", str(BIN / "ollama-queue.py"), "escalate", str(job_id),
            "--reason", reason, "--category", category,
            "--coordinator-model", "auto-fix-ladder",
            "--gate-output", str(gate_json)]


def autofix_fingerprint(payload: dict) -> tuple[int, list]:
    """PURE. A durable, comparable summary of a round's gate findings, used by the
    anti-thrash guard to tell whether an auto-fix round actually made progress.
    Returns (total, sorted_finding_hashes):
      - total: the gate's own finding count (counts.total), falling back to
        len(issues). This is the number the guard requires to STRICTLY decrease
        from one round to the next.
      - a stable per-finding hash `source|file|what[:80]` for every issue, so an
        identical finding-set across rounds is recognisable even when the count is
        unchanged (the 'same finding fixed twice' case)."""
    issues = payload.get("issues") or []
    counts = payload.get("counts") or {}
    total = counts.get("total")
    if not isinstance(total, int):
        total = len(issues)
    fp = sorted(
        f"{i.get('source','?')}|{i.get('file','')}|{str(i.get('what',''))[:80]}"
        for i in issues)
    return total, fp


def _autofix_ledger_path(root: str) -> Path:
    return STATE_DIR / "ollama-queue-logs" / "auto-fix" / f"{_safe_id(root)}.rounds.json"


def _safe_id(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(s))[:120] or "root"


def _autofix_ledger_read(root: str) -> dict:
    """The per-CHAIN round ledger, keyed by auto_fix_root. Durable on disk so the
    anti-thrash comparison survives a daemon restart. Fail-open: unreadable -> {}."""
    try:
        p = _autofix_ledger_path(root)
        if p.is_file():
            d = json.loads(p.read_text())
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _autofix_ledger_record(root: str, rnd: int, job_id: str,
                           total: int, fp: list, model: str = "") -> None:
    """Record this round's fingerprint (+ the model that produced it) into the chain
    ledger. Keyed by round, so a re-run of the gate for the same round OVERWRITES
    (idempotent) rather than duplicating. The model is what lets a later round tell
    'the SAME findings came from a DIFFERENT model' (suspect-the-grader). Never
    raises."""
    try:
        p = _autofix_ledger_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        d = _autofix_ledger_read(root)
        rounds = d.get("rounds")
        if not isinstance(rounds, dict):
            rounds = {}
        rounds[str(rnd)] = {
            "job_id": job_id, "total": total, "fingerprint": fp, "model": model,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        d["root"] = root
        d["rounds"] = rounds
        p.write_text(json.dumps(d, indent=1))
    except Exception:
        pass


def autofix_thrash_check(root: str, rnd: int, cur_total: int,
                         cur_fp: list) -> str | None:
    """ANTI-THRASH. Decide whether a would-be requeue of round `rnd` -> `rnd+1`
    must instead be PARKED for a human because the previous auto-fix round did not
    make progress. Returns an escalation reason string to BLOCK the requeue, or
    None to allow it. Fail-open: no parent record (or unreadable ledger) -> None
    (the round cap still bounds the chain).

    Two independent park triggers, either of which fires:
      (1) SAME FINDINGS: round `rnd` reproduced byte-for-byte the finding-set of
          round `rnd-1` -- the fix changed nothing the gate can see. Catches the
          'auto-remediate the same finding twice' case even at equal counts.
      (2) NON-REDUCING: round `rnd`'s finding count did not STRICTLY drop below
          round `rnd-1`'s. A requeue that isn't shrinking the problem is thrash.
    Only rounds >= 1 have a parent to compare against; round 0 always returns None."""
    if rnd < 1:
        return None
    prev = (_autofix_ledger_read(root).get("rounds") or {}).get(str(rnd - 1))
    if not isinstance(prev, dict):
        return None
    prev_fp = prev.get("fingerprint") or []
    prev_total = prev.get("total")
    if cur_fp and sorted(cur_fp) == sorted(prev_fp):
        return (f"auto-fix round r{rnd} reproduced the SAME findings as r{rnd - 1} "
                f"(the previous fix changed nothing the gate sees); parking for human")
    if (isinstance(prev_total, int) and isinstance(cur_total, int)
            and cur_total >= prev_total):
        return (f"auto-fix round r{rnd} did not reduce the finding count "
                f"({prev_total} -> {cur_total}); parking for human")
    return None


def autofix_grader_suspect(root: str, rnd: int, cur_fp: list,
                           cur_model: str) -> str | None:
    """SUSPECT-THE-GRADER, early. Return the name of a PRIOR-round model that
    produced the SAME finding fingerprint as this round under a DIFFERENT model --
    the strong signal that the fault is the fixture/spec, not model capability, so
    the model-fallback ladder should STOP rather than climb to a bigger (e.g. 70B)
    rung that will fail identically. None if no distinct model has reproduced this
    fingerprint. Fail-open: unreadable ledger / empty fp -> None (the round cap and
    the ladder-exhaustion park still bound the chain).

    Unlike autofix_thrash_check (adjacent rounds only), this scans ALL recorded
    rounds except `rnd` itself, so it fires the moment the 2nd distinct model repeats
    a finding-set -- even if an intervening round differed."""
    if not cur_fp:
        return None
    cur_sorted = sorted(cur_fp)
    rounds = (_autofix_ledger_read(root).get("rounds") or {})
    for k, rec in rounds.items():
        if k == str(rnd) or not isinstance(rec, dict):
            continue
        m = rec.get("model") or ""
        if m and m != cur_model and sorted(rec.get("fingerprint") or []) == cur_sorted:
            return m
    return None



_HARNESS_FB = {"TASK.md", "verify.sh", "check_literals.py", "refimpl.py", "verify.test.ts",
               ".dispatch-harness.json", "auto-harness-check.py", "AUTO-TASK.md"}


def _near_miss_feedback(cwd, issues, limit=8, diff_path=None):
    """['- `have` ... `want`'] for each completeness-absent literal whose identifier
    tokens appear in the job's edited files only under a near-identical spelling.

    The job's RECORDED diff (diff_path, <out_dir>/<job>.diff) is the evidence when it
    exists; the live worktree is only the fallback. Reading the live tree (canary soak
    seed 26, 2026-10-06) raced a concurrent self-check that had the refimpl applied:
    the tree then HAD the spec spelling `clampHigh`, so the model's `clampHihg` was
    never named and the auto-fix round was told only "absent from the diff"."""
    import difflib
    wants = []
    for i in issues:
        m = re.search(r"spec names `(.+?)` but it is absent", str(i.get("what", "")))
        if m:
            wants += re.findall(r"[A-Za-z_][A-Za-z0-9_]{5,}", m.group(1))
    if not wants or not (cwd or diff_path):
        return []
    text = ""
    try:
        _dp = Path(str(diff_path)).expanduser() if diff_path else None
        if _dp is not None and _dp.is_file() and _dp.stat().st_size:
            text = "\n".join(l[1:] for l in _dp.read_text(errors="replace").splitlines()
                             if l.startswith("+") and not l.startswith("+++"))
    except OSError:
        text = ""
    wt = Path(str(cwd)).expanduser() if cwd else None
    try:
        if text or wt is None:
            raise StopIteration
        r = subprocess.run(["git", "-C", str(wt), "diff", "HEAD"], capture_output=True,
                           text=True, timeout=60)
        text += "\n".join(l[1:] for l in r.stdout.splitlines()
                          if l.startswith("+") and not l.startswith("+++"))
        r = subprocess.run(["git", "-C", str(wt), "ls-files", "--others", "--exclude-standard"],
                           capture_output=True, text=True, timeout=60)
        for rel in r.stdout.split():
            if Path(rel).name in _HARNESS_FB:
                continue
            try:
                text += "\n" + (wt / rel).read_text(errors="replace")[:200000]
            except OSError:
                pass
    except StopIteration:
        pass                       # the recorded diff is the evidence
    except Exception:
        return []
    have = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text))
    out, seen = [], set()
    for w in dict.fromkeys(wants):
        if w in have:
            continue
        for h in sorted(have):
            hl, wl = h.lower(), w.lower()
            if (abs(len(h) - len(w)) > 2 or hl[:3] != wl[:3] or wl in hl or hl in wl
                    or difflib.SequenceMatcher(None, hl, wl).ratio() < 0.8 or (h, w) in seen):
                continue
            seen.add((h, w))
            # keep the written token's leading case: `orderEgmtLink` (the Prisma
            # client accessor) must become `orderEgiftLink`, not `OrderEgiftLink`
            fix = (w[:1].lower() if h[:1].islower() else w[:1].upper()) + w[1:]
            out.append(f"- you wrote `{h}` where the spec spelling is `{w}` -- rename EVERY "
                       f"`{h}` to `{fix}` in all edited files; the implementation "
                       f"itself may already be right")
    return out[:limit]


def autofix_build_requeue(job_id: str, payload: dict, out_dir: Path,
                          next_round: int, root: str, dec: dict,
                          override_model: str | None = None,
                          override_host: str | None = None,
                          models_tried: list | None = None) -> tuple[list, Path]:
    """Build (enqueue_argv, feedback_file) for an auto-fix requeue. Writes the
    guidance file; does NOT enqueue. The command is also the exact shadow preview.

    CONSTRAINT #1 (the sealed contract is inviolable): the requeue re-uses the
    SAME --cwd (the original worktree) and the SAME --verify (the sealed
    verify.sh). The gate concerns are injected as a SEPARATE guidance file that
    becomes the requeue's --task-file, built as `<original TASK.md text> + a
    '## Gate feedback from previous attempt' section`, written OUTSIDE the sealed
    worktree (in ollama-queue-logs). verify.sh, the fixtures and the worktree
    TASK.md are left BYTE-IDENTICAL -- the model literally cannot reach its own
    verify through this path, and the scope audit / verify-relevance still bite
    if it edits the tracked verify anyway.

    MODEL FALLBACK (Tier 1): when `override_model` is set the requeue swaps the
    model (and host) to the next ladder rung instead of re-running the model that
    just failed. Everything else -- the sealed cwd/verify, the round/root markers,
    the num_ctx/max_iters/task_kind -- is UNCHANGED; a different model still faces
    the identical gate. `models_tried` is threaded through so the chain remembers
    which rungs it has burned (loop guard + escalation reason)."""
    facts = job_facts(job_id)
    cwd = facts.get("job_cwd") or payload.get("job_cwd") or ""
    verify = facts.get("job_verify") or payload.get("job_verify") or ""
    prev_model = _job_field(job_id, "model") or facts.get("job_model") or ""
    model = override_model or prev_model
    host = override_host if override_model else (_job_field(job_id, "host_pref") or "")
    num_ctx = _job_field(job_id, "num_ctx")
    max_iters = _job_field(job_id, "max_iters")
    task_kind = _job_field(job_id, "task_kind")
    orig_label = str(facts.get("job_label") or _job_field(job_id, "label") or job_id)
    # strip any prior [auto-fix rN] so the label doesn't accrete markers
    base_label = re.sub(r"\s*\[auto-fix r\d+\]$", "", orig_label)
    # Original TASK text: the job record's task_file (live row, then the durable
    # sidecar), then the payload's own task_file, then the worktree's TASK.md.
    # (Audit 2026-09-23: the last fallback used to be payload["job_cwd"] -- a
    # DIRECTORY, so is_file() was always False and a requeue built without a
    # readable task_file carried ONLY the gate-feedback section, no task.)
    _task_cands = [_job_field(job_id, "task_file"), payload.get("task_file")]
    if cwd:
        _task_cands.append(Path(str(cwd)).expanduser() / "TASK.md")

    # Build the feedback-augmented task file OUTSIDE the sealed worktree.
    fb_dir = out_dir / "auto-fix"
    fb_dir.mkdir(parents=True, exist_ok=True)
    fb_file = fb_dir / f"{job_id}-r{next_round}.task.md"
    orig_text = ""
    for _cand in _task_cands:
        try:
            if _cand and Path(str(_cand)).expanduser().is_file():
                orig_text = Path(str(_cand)).expanduser().read_text(errors="replace")
                break
        except Exception:
            continue
    concerns = "\n".join(f"- {r}" for r in (dec.get("reasons") or []))
    top = ""
    for i in (payload.get("issues") or []):
        if i.get("severity") == "high":
            loc = f"{i.get('file','')}:{i.get('line','')}".strip(":")
            top += f"- [{str(i.get('severity')).upper()}] {loc} ({i.get('source')}): {str(i.get('what',''))[:200]}\n"
    # NEAR-MISS SPELLING (2026-10-05, rt-egift-link-s1-s0 0ea47b5f09f8): the model
    # implemented the whole spec under `OrderEgmtLink` instead of `OrderEgiftLink`;
    # the completeness findings only said "absent from the diff", so the next round
    # would hunt for a missing feature. Name the token the tree actually has.
    try:
        _near = _near_miss_feedback(cwd, payload.get("issues") or [],
                                    diff_path=Path(out_dir) / f"{job_id}.diff")
    except Exception:
        _near = []
    if _near:
        top += ("\nSPELLING, not missing work -- your edit has a near-identical name:\n"
                + "\n".join(_near) + "\n")
    swap_note = ""
    if override_model:
        swap_note = (f"\nNOTE: a previous model ({prev_model}) could not clear this "
                     f"gate; you are a different model retrying the SAME task and the "
                     f"SAME sealed verify.sh.\n")
    fb_section = (
        "\n\n## Gate feedback from previous attempt (auto-fix round "
        f"{next_round})\n\n"
        "Your previous attempt did NOT clear the automated gate "
        f"(class: {dec.get('class')}). Fix the following and re-run "
        "`bash verify.sh` until it prints VERIFY_OK. HARD RULE: edit ONLY the "
        "target source file(s). Do NOT edit, weaken, delete, or add cases to any "
        "of: verify.sh, the gate, check_literals.py, TASK.md, or any fixture/test "
        "file. Making the gate pass by touching the harness is a REJECTED 'fix' -- "
        "the scope audit and verify-relevance will catch it and it will not land.\n"
        f"{swap_note}\n"
        f"{concerns}\n"
        + (("\nHigh-severity findings:\n" + top) if top else ""))
    try:
        fb_file.write_text((orig_text or "") + fb_section)
    except Exception:
        pass

    argv = ["python3", str(BIN / "ollama-queue.py"), "enqueue",
            "--model", str(model), "--cwd", str(cwd),
            "--task-file", str(fb_file),
            "--label", f"{base_label} [auto-fix r{next_round}]",
            "--auto-fix-round", str(next_round), "--auto-fix-root", str(root)]
    if models_tried:
        argv += ["--models-tried", ",".join(str(m) for m in models_tried)]
    # Explicit queue bundle tag is inherited: an auto-fix round is the SAME
    # piece of work as the job it retries, so it must stay in that job's bundle
    # (ollama-queue.py job_group_key honours `bundle` over label parsing).
    _bundle = _job_field(job_id, "bundle")
    if _bundle:
        argv += ["--bundle", str(_bundle)]
    if host:
        argv += ["--host", str(host)]
    if verify:
        argv += ["--verify", str(verify)]
    if isinstance(num_ctx, int):
        argv += ["--num-ctx", str(num_ctx)]
    if isinstance(max_iters, int):
        argv += ["--max-iters", str(max_iters)]
    if task_kind:
        argv += ["--task-kind", str(task_kind)]
    return argv, fb_file


def _autofix_log(out_dir: Path, job_id: str, payload: dict) -> None:
    """Append the auto-fix decision to an at-a-glance worklist. Never raises."""
    try:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        act = payload.get("auto_fix_action")
        row = (f"- `{job_id}` **{act}** ({payload.get('auto_fix_class')}) "
               f"round={payload.get('auto_fix_round')} mode={payload.get('auto_fix_mode')} "
               f"verdict={payload.get('verdict')} - {stamp}\n")
        for r in (payload.get("auto_fix_reasons") or [])[:4]:
            row += f"    - {r}\n"
        if payload.get("auto_fix_cmd_preview"):
            row += "    - requeue: `" + " ".join(payload["auto_fix_cmd_preview"]) + "`\n"
        if payload.get("auto_fix_escalate_cmd"):
            row += "    - escalate: `" + " ".join(payload["auto_fix_escalate_cmd"]) + "`\n"
        with (out_dir / "AUTO-FIX-QUEUE.md").open("a") as fh:
            fh.write(row)
    except Exception:
        pass


def autofix_consider(job_id: str, payload: dict, gate_json: Path,
                     out_dir: Path) -> None:
    """Decide + record (+ in live mode, enqueue) the auto-fix for a TERMINAL
    verdict. Advisory throughout: never raises, never changes the exit code.

    SHADOW (default): writes the decision, the round, and the exact requeue
    COMMAND into .gate.json and AUTO-FIX-QUEUE.md, and enqueues nothing.
    LIVE: additionally runs the enqueue -- ONCE (guarded by auto_fix_enqueued).
    """
    try:
        # AUTHORING-JOB GUARD (2026-09-18). An `auto-author-*`/`auto-refine-*` job is
        # ollama-dispatch-auto's HARNESS-authoring meta-task, not a coding dispatch.
        # Its launch tree is EXPECTEDLY dirty (the scaffold seeds an untracked target
        # stub) and it EXPECTEDLY edits verify.sh/refimpl/fixture -- so the coding-gate
        # findings [launch-baseline, scope, verify-exit] are category errors against it.
        # Escalating them to needs_opus does real harm: it flips the queue row to a
        # non-terminal parked state the orchestrator's poller (which waits for
        # done/failed) cannot consume, STRANDING the whole slice (the broker-guard
        # batch, 2026-09-18). The authoring refine loop is owned INLINE by
        # ollama-dispatch-auto (poll->preflight->refine->re-slice); the gate must never
        # requeue or escalate an authoring job. Record the observation and return.
        if _is_authoring_label(payload.get("job_label")
                               or _job_field(job_id, "label")):
            payload["auto_fix_class"] = "authoring-orchestrator-owned"
            payload["auto_fix_action"] = "none"
            payload["auto_fix_reasons"] = [
                "authoring/refine meta-task: launch-baseline/scope/verify-exit are "
                "expected for a harness author; the ollama-dispatch-auto orchestrator "
                "owns the inline refine loop. Gate does NOT escalate or requeue."]
            payload["auto_fix_round"] = autofix_round(job_id, payload)
            payload["auto_fix_root"] = autofix_root(job_id, payload)
            payload["auto_fix_mode"] = AUTOFIX_MODE
            gate_json.write_text(json.dumps(payload, indent=1))
            return
        # INVESTIGATION GUARD (2026-10-02): a diagnosis/research job has no code to
        # fix -- its deliverable is DIAGNOSIS.md / an answer. Never requeue a fix
        # round or escalate it from the auto-fix path; a human reads the finding.
        if _job_is_investigation(job_id, payload):
            payload["auto_fix_class"] = "investigation-no-code"
            payload["auto_fix_action"] = "none"
            payload["auto_fix_reasons"] = [
                "diagnosis/investigation job: the deliverable is a written finding, "
                "not a code diff -- no auto-fix round is ever enqueued for it."]
            payload["auto_fix_round"] = autofix_round(job_id, payload)
            payload["auto_fix_root"] = autofix_root(job_id, payload)
            payload["auto_fix_mode"] = AUTOFIX_MODE
            gate_json.write_text(json.dumps(payload, indent=1))
            return
        dec = autofix_classify(payload)
        try:
            dec = harness_refine_apply(dec, payload, payload.get("job_label") or _job_field(job_id, "label"))
        except Exception:
            pass        # advisory conversion: any failure keeps the original (park) decision
        # PLAN-GEN GUARD (2026-10-05, rt-egift-link-s1). A `plan-gen-*` job is one
        # round of ollama-dispatch-plan, which OWNS its own bounded round loop (it
        # re-gates plan.json and re-prompts with the specific defects). A gate
        # requeue here is a SECOND, parallel retry in the SAME scratch dir: f1cf7fa2ebab
        # (r1) was requeued as 95e0a836a3ef while plan-gen had already launched r2
        # (0d3ee0001f87); the requeue ran on r2's finished plan, wrote nothing, and was
        # failed as a vacuous pass (class=model "stopped at iteration 5/20"). Never
        # requeue a plan-gen round; escalation (a human look) is left untouched.
        if dec.get("action") == "requeue" and _is_plangen_label(
                payload.get("job_label") or _job_field(job_id, "label")):
            dec = {"class": "plangen-orchestrator-owned", "action": "none",
                   "reasons": ["plan-gen round: ollama-dispatch-plan owns the round/"
                               "refine loop (re-gate + re-prompt in the same scratch "
                               "dir); a gate requeue would race its next round. "
                               "Not requeued."] + list(dec.get("reasons") or [])[:3]}
        rnd = autofix_round(job_id, payload)
        root = autofix_root(job_id, payload)
        payload["auto_fix_class"] = dec["class"]
        payload["auto_fix_action"] = dec["action"]
        payload["auto_fix_reasons"] = (dec.get("reasons") or [])[:6]
        payload["auto_fix_round"] = rnd
        payload["auto_fix_root"] = root
        payload["auto_fix_mode"] = AUTOFIX_MODE
        cur_total, cur_fp = autofix_fingerprint(payload)
        # The model that just ran, and the rungs this chain has already burned.
        prev_tried = autofix_models_tried(job_id, payload)
        cur_model = str(_job_field(job_id, "model") or job_facts(job_id).get("job_model") or "")
        payload["models_tried"] = prev_tried + ([cur_model] if cur_model
                                                and cur_model not in prev_tried else [])
        # Record THIS round's finding fingerprint (+ the model that produced it) into
        # the chain ledger BEFORE deciding, so a future round (and the re-run of this
        # one) can compare against it. Idempotent (keyed by round); fail-open.
        _autofix_ledger_record(root, rnd, job_id, cur_total, cur_fp, cur_model)
        next_round = rnd + 1
        # EARLY SUSPECT-THE-GRADER: has this exact finding-set already recurred under a
        # DIFFERENT model? If so, no further ladder rung can help (the fault is the
        # fixture/spec, not capability) -- park now instead of climbing the rest of the
        # roster (e.g. up to a 70B) on a task no model can clear.
        _grader_suspect_model = (autofix_grader_suspect(root, rnd, cur_fp, cur_model)
                                 if cur_fp else None)
        # CAPABILITY-EXHAUSTION of the CURRENT model: a would-be requeue blocked by
        # the round cap (LOOP GUARD 1) or by the anti-thrash guard (LOOP GUARD 2).
        # These -- and ONLY these -- are where the model-fallback ladder engages: a
        # capability failure where a DIFFERENT model might converge. A judgment
        # failure (classify already returned `escalate`) never reaches here, so it
        # skips the ladder and parks straight for Opus (the tier boundary).
        _exhausted, _exhaust_reason = False, None
        if dec["action"] == "requeue" and next_round > AUTOFIX_MAX_ROUNDS:
            _exhausted = True
            _exhaust_reason = (f"model {cur_model or '?'} hit the self-fix cap "
                               f"(round {next_round} > {AUTOFIX_MAX_ROUNDS})")
        if dec["action"] == "requeue" and not _exhausted:
            _thrash = autofix_thrash_check(root, rnd, cur_total, cur_fp)
            if _thrash:
                _exhausted = True
                _exhaust_reason = f"model {cur_model or '?'} thrashed: {_thrash}"
        # A grader-suspect signal (the SAME finding-set from >=2 DISTINCT models)
        # forces the park path regardless of the round cap, so it fires the moment a
        # 2nd model repeats a finding-set -- before the ladder climbs to the expensive
        # rungs. It also suppresses the ladder below (nxt forced to None).
        if dec["action"] == "requeue" and _grader_suspect_model:
            _exhausted = True
            if not _exhaust_reason:
                _exhaust_reason = (f"identical findings from distinct models "
                                   f"({cur_model or '?'}, {_grader_suspect_model})")
        argv = None            # a same-model or model-fallback requeue enqueue argv
        esc_argv = None        # a needs_opus escalate (park) argv
        if _exhausted:
            # TIER 1: retry the SAME sealed task+verify with the NEXT ladder rung --
            # UNLESS the same finding-set already recurred under a different model, in
            # which case a bigger model cannot help and we force the park (nxt=None).
            ladder = load_model_ladder()
            nxt = (None if _grader_suspect_model
                   else plan_model_ladder(cur_model, prev_tried, ladder, served=served_model,
                                          current_host=str(_job_field(job_id, "host_pref") or "studio")))
            if nxt:
                dec["action"] = "requeue"
                payload["auto_fix_action"] = "requeue"
                payload["auto_fix_class"] = "model-fallback"
                dec["class"] = "model-fallback"
                payload["auto_fix_fallback_model"] = nxt["model"]
                payload["auto_fix_fallback_host"] = nxt["host"]
                payload["auto_fix_reasons"] = (
                    [f"{_exhaust_reason}; falling back to next ladder model "
                     f"{nxt['model']}@{nxt['host']} (same task+verify, full gate)"]
                    + payload["auto_fix_reasons"])[:6]
                argv, _fb = autofix_build_requeue(
                    job_id, payload, out_dir, next_round, root, dec,
                    override_model=nxt["model"], override_host=nxt["host"],
                    models_tried=payload["models_tried"])
                payload["auto_fix_next_round"] = next_round
                payload["auto_fix_cmd_preview"] = argv
            elif _grader_suspect_model:
                # TIER 2 (early): the SAME finding-set came back from >=2 DISTINCT
                # models. A bigger model will not clear it -- park now WITHOUT burning
                # the rest of the ladder. This is the whole point of the fix.
                dec["action"] = "escalate"
                payload["auto_fix_action"] = "escalate"
                payload["auto_fix_class"] = "grader-suspect"
                dec["class"] = "grader-suspect"
                payload["auto_fix_reasons"] = (
                    [f"SUSPECT THE GRADER: identical gate findings from >=2 distinct "
                     f"models ({cur_model or '?'} and {_grader_suspect_model}) -- "
                     f"inspect the fixture/spec; a bigger model will not clear it. "
                     f"Parked needs_opus without climbing the rest of the ladder "
                     f"(tried: {', '.join(payload['models_tried']) or '(none)'})."]
                    + payload["auto_fix_reasons"])[:6]
            else:
                # TIER 2: the ladder is exhausted -> park as needs_opus.
                dec["action"] = "escalate"
                payload["auto_fix_action"] = "escalate"
                payload["auto_fix_class"] = "ladder-exhausted"
                dec["class"] = "ladder-exhausted"
                # SUSPECT-THE-GRADER cross-check (Task D): >=2 DISTINCT models could
                # not clear the SAME sealed gate. That is the strong signal it is the
                # grader/spec/harness at fault, not model incapacity -- flag it in the
                # human-read reason so the Opus drain looks at the fixture first. The
                # category stays persistent-nogo (real attempts were made), but the
                # diagnosis is recorded up front.
                _distinct = len(payload["models_tried"])
                _suspect = ([f"SUSPECT THE GRADER: {_distinct} distinct models could "
                             f"not clear the SAME gate -- inspect the fixture/spec "
                             f"before concluding model incapacity"]
                            if _distinct >= 2 else [])
                payload["auto_fix_reasons"] = (
                    _suspect
                    + [f"{_exhaust_reason}; model-fallback ladder EXHAUSTED (tried: "
                       f"{', '.join(payload['models_tried']) or '(none)'}); parking "
                       f"needs_opus"] + payload["auto_fix_reasons"])[:6]
        elif dec["action"] == "requeue":
            # TIER 0 (unchanged): same-model self-fix requeue under the cap.
            argv, _fb = autofix_build_requeue(job_id, payload, out_dir,
                                              next_round, root, dec)
            payload["auto_fix_next_round"] = next_round
            payload["auto_fix_cmd_preview"] = argv
        # TIER 2 escalate command (both a judgment-class direct escalate AND a
        # ladder-exhausted one): reuse the existing escalate machinery. persistent-
        # nogo for capability exhaustion, mapped category for a judgment class.
        if dec["action"] == "escalate":
            _cat = autofix_escalation_category(payload["auto_fix_class"])
            # Hand the drain the FULL repair journey (Task D): the classified reason
            # PLUS the whole-chain ledger summary (per-round findings + models tried).
            _hist = autofix_history(root, payload.get("models_tried"))
            payload["auto_fix_history"] = _hist
            _reason = ("; ".join(str(r) for r in payload["auto_fix_reasons"][:3])
                       or f"gate {payload.get('verdict')}: needs an Opus judgment") \
                + " | " + _hist
            esc_argv = autofix_build_escalate(job_id, root, _reason, _cat, gate_json)
            payload["auto_fix_escalate_category"] = _cat
            payload["auto_fix_escalate_cmd"] = esc_argv
        gate_json.write_text(json.dumps(payload, indent=1))
        _autofix_log(out_dir, job_id, payload)
        print(f"[gate] {job_id} auto-fix={payload['auto_fix_action']} "
              f"class={payload['auto_fix_class']} round={rnd} mode={AUTOFIX_MODE}")
        # LIVE, once. Off by default; only reached when explicitly enabled.
        # A requeue enqueues the next attempt; an escalate PARKS as needs_opus.
        if AUTOFIX_MODE == "live" and not TEST_MODE:
            if (dec["action"] == "requeue" and argv is not None
                    and not payload.get("auto_fix_enqueued")):
                try:
                    e = subprocess.run(argv, capture_output=True, text=True, timeout=120)
                    payload["auto_fix_enqueued"] = e.returncode == 0
                    payload["auto_fix_enqueue_status"] = (
                        "requeued" if e.returncode == 0 else "enqueue-failed")
                    if e.returncode != 0:
                        payload["auto_fix_enqueue_error"] = (e.stderr or e.stdout or "")[-200:]
                except Exception as ex:
                    payload["auto_fix_enqueue_status"] = "enqueue-failed"
                    payload["auto_fix_enqueue_error"] = f"{type(ex).__name__}: {str(ex)[:160]}"
                gate_json.write_text(json.dumps(payload, indent=1))
            elif (dec["action"] == "escalate" and esc_argv is not None
                    and not payload.get("auto_fix_escalated")):
                try:
                    e = subprocess.run(esc_argv, capture_output=True, text=True, timeout=120)
                    payload["auto_fix_escalated"] = e.returncode == 0
                    payload["auto_fix_escalate_status"] = (
                        "parked-needs_opus" if e.returncode == 0 else "escalate-failed")
                    if e.returncode != 0:
                        payload["auto_fix_escalate_error"] = (e.stderr or e.stdout or "")[-200:]
                except Exception as ex:
                    payload["auto_fix_escalate_status"] = "escalate-failed"
                    payload["auto_fix_escalate_error"] = f"{type(ex).__name__}: {str(ex)[:160]}"
                gate_json.write_text(json.dumps(payload, indent=1))
    except Exception as e:
        try:
            payload["auto_fix_error"] = f"{type(e).__name__}: {str(e)[:180]}"
            gate_json.write_text(json.dumps(payload, indent=1))
        except Exception:
            pass


# ==============================================================================
# ESCALATION TRIAGE (Task E). The drain side of the needs_opus lane: decide, with
# NO model call, whether a reviewer-code-high park is the proven intended-removal
# false-FAIL class. PURE decision + a thin, idempotent, shadow-gated effector.
# FAIL-CLOSED everywhere: every unreadable/ambiguous input parks.
# ==============================================================================
def _triage_norm(s) -> str:
    """Whitespace-collapsed, separator-stripped form of a code line, so a quote and
    the same line in the diff/TASK compare equal across reflow and a trailing comma."""
    return re.sub(r"\s+", " ", str(s or "")).strip().strip(",;")


def _longest_common_run(needle: str, hay: str) -> int:
    """Length of the longest contiguous run of `needle` that occurs in `hay`. Used as
    the CONTRACT CITATION test: TASK.md must itself quote a long, contiguous piece of
    the removed literal. Substring search is C-fast; quotes are short."""
    needle, hay = str(needle or ""), str(hay or "")
    best, n = 0, len(needle)
    for i in range(n):
        if n - i <= best:
            break
        L = best
        while i + L + 1 <= n and needle[i:i + L + 1] in hay:
            L += 1
        if L > best:
            best = L
    return best


def _triage_diff_lines(diff_text: str):
    """(removed, added) normalised content lines of a unified diff. File headers
    (---/+++) are NOT content and must never be mistaken for a removed line."""
    removed, added = [], []
    for raw in str(diff_text or "").splitlines():
        if raw.startswith("---") or raw.startswith("+++"):
            continue
        if raw.startswith("-"):
            v = _triage_norm(raw[1:])
            if v:
                removed.append(v)
        elif raw.startswith("+"):
            v = _triage_norm(raw[1:])
            if v:
                added.append(v)
    return removed, added


# --- STAGE 4 helpers: CONTRACT SUPERSESSION (pure) ----------------------------
_TRIAGE_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")
# Identifiers, dotted names and `--flags` -- the things a rewrite can DROP. The
# {2,} tail means a token is >=3 chars, so `a`, `k`, `fh` never drive a decision.
_TRIAGE_TOKEN_RE = re.compile(r"(?:--)?[A-Za-z_][A-Za-z0-9_.\-]{2,}")


def _triage_signature_rewrites(diff_text: str) -> list:
    """Every function this diff REDEFINES, with the body lines attributed to it:
    a `-def NAME(...)` superseded by a `+def NAME(...)` inside the SAME hunk, plus
    the removed/added lines that follow it. Attribution NEVER crosses a hunk
    boundary (`@@`) or a file boundary -- the lines either side of a gap are not
    known to be in the same function, and guessing there is how a rule overfires.
    Returns [{file, name, old, new, removed[], added[]}] with old/new normalised."""
    regions, cur, path = [], None, ""
    for raw in str(diff_text or "").splitlines():
        if raw.startswith("diff --git"):
            m = re.search(r" b/(\S+)$", raw)
            path, cur = (m.group(1) if m else ""), None
            continue
        if raw.startswith("---") or raw.startswith("+++") or raw.startswith("index "):
            continue
        if raw.startswith("@@"):
            cur = None
            continue
        tag, content = raw[:1], raw[1:]
        if tag not in (" ", "+", "-"):
            continue
        m = _TRIAGE_DEF_RE.match(content)
        if m:
            name, norm = m.group(1), _triage_norm(content)
            # The `+def` that PAIRS with the `-def` we just opened -- same function,
            # replacement signature. git emits these within a few lines of each
            # other, possibly with removed docstring lines in between.
            if (cur is not None and tag == "+" and cur["name"] == name
                    and cur["old"] and not cur["new"]):
                cur["new"] = norm
                continue
            cur = {"file": path, "name": name, "removed": [], "added": [],
                   "old": norm if tag == "-" else None,
                   "new": norm if tag == "+" else None}
            regions.append(cur)
            continue
        if cur is None:
            continue
        v = _triage_norm(content)
        if not v:
            continue
        if tag == "-":
            cur["removed"].append(v)
        elif tag == "+":
            cur["added"].append(v)
    return regions


def _triage_must_not_change(task_text: str) -> str:
    """TASK.md's 'Behaviour that must NOT change' block, normalised + lowercased.
    This is the contract's explicit PRESERVE list. A token the diff dropped that is
    named here is collateral damage, not a mandated removal, so it must still park.
    Matches the heading whether it is a `##` heading or a plain in-section line, and
    runs to the next heading (over-capturing is the safe direction: more parks)."""
    out, on = [], False
    for ln in str(task_text or "").splitlines():
        if re.search(r"behaviou?r\s+that\s+must\s+not\s+change", ln, re.I):
            on = True
            continue
        if on:
            if re.match(r"^\s*#{1,6}\s", ln):
                break
            out.append(ln)
    return _triage_norm(" ".join(out)).lower()


def _triage_contract_supersession(quote: str, regions, tnorm: str, task_text: str,
                                  add_join: str, *, min_sig: int | None = None):
    """STAGE 4 test, PURE. Is normalised `quote` a line this diff removed as part of
    a TASK-DICTATED whole-signature rewrite? Returns None when no rewrite covers the
    quote at all, else {ok, func, where, dropped, ...} -- ok False when the PRESERVE
    guard bit. Never raises. Advisory: the caller must still only ever park on it."""
    min_sig = ESCALATION_TRIAGE_MIN_SIG if min_sig is None else min_sig
    for reg in (regions or []):
        old, new = reg.get("old"), reg.get("new")
        # Both signatures must exist and DIFFER -- otherwise nothing was redesigned.
        if not old or not new or old == new or len(new) < min_sig:
            continue
        # THE LOAD-BEARING CHECK: TASK.md dictates the replacement signature as a
        # whole, exact, contiguous line. A paraphrase or a partial overlap fails.
        if new not in tnorm:
            continue
        if quote == old:
            where = f"the OLD signature of {reg['name']}(), replaced wholesale"
        elif quote in (reg.get("removed") or []):
            where = f"a line in the rewritten body of {reg['name']}()"
        else:
            continue
        # What this line dropped that the diff did not put back ANYWHERE.
        dropped = sorted({t for t in _TRIAGE_TOKEN_RE.findall(quote)
                          if t not in add_join})
        kept = [t for t in dropped if t.lower() in _triage_must_not_change(task_text)]
        base = {"func": reg["name"], "file": reg.get("file"), "where": where,
                "dropped": dropped[:8], "new_signature": new[:180]}
        if kept:
            # TASK.md explicitly promised to PRESERVE something this line dropped.
            return dict(base, ok=False, preserved_by_contract=kept[:6])
        return dict(base, ok=True)
    return None


def classify_escalation_triage(payload: dict, findings, task_text: str,
                               diff_text: str, *, min_cite: int | None = None) -> dict:
    """PURE. Given a gate payload that just escalated, the reviewer's structured
    findings (each with the verbatim `quote` it flagged), the job's FULL TASK text
    and its diff, decide {action, class, reasons, citations}.

    action:
      skip         -- not a reviewer-code-high escalation; nothing to triage.
      park         -- stays needs_opus (the default for everything uncertain).
      auto-resolve -- PROVEN intended-removal false FAIL: every flagged literal is
                      a line this diff removed and never re-added, in a file the
                      TASK names, TASK.md itself quotes it, and the verify is
                      green, proven-red at baseline and relevance-proven.
    Never raises, never performs I/O."""
    min_cite = ESCALATION_TRIAGE_MIN_CITE if min_cite is None else min_cite
    if str(payload.get("auto_fix_class") or "") != "reviewer-code-high":
        return {"action": "skip", "class": "not-reviewer-code-high", "citations": [],
                "reasons": ["only the reviewer-code-high park class is triaged"]}
    if str(payload.get("auto_fix_action") or "") != "escalate":
        return {"action": "skip", "class": "not-an-escalation", "citations": [],
                "reasons": ["the ladder did not escalate this job"]}

    hard = []

    def need(cond, why):
        if not cond:
            hard.append(why)

    counts = payload.get("counts") or {}
    vr = payload.get("verify_relevance") or {}
    lb = payload.get("launch_baseline") or {}

    def _int(v, d=0):
        try:
            return int(v)
        except Exception:
            return d

    # (1) The verify must be GREEN, NON-VACUOUS and RELEVANT. This is the whole
    # basis for overriding a reviewer: real behavioural evidence beats a prose
    # claim ONLY when that evidence was proven to discriminate.
    need(payload.get("verify_exit_reported") == 0, "verify did not report exit 0")
    need(payload.get("verify_quality_exit") in (0, None), "verify-quality exit is non-zero")
    need(payload.get("job_exit_code") == 0, "job exit code is not 0")
    need(payload.get("verify_failed_at_baseline") is True,
         "verify was NOT proven to fail at baseline (it could be vacuous)")
    need(str(vr.get("verdict") or "") == "relevant", "verify_relevance verdict is not 'relevant'")
    need(_int(vr.get("survived"), 1) == 0 and _int(vr.get("killed")) > 0,
         "verify_relevance has surviving mutants or killed none")
    need(not vr.get("truncated"), "verify_relevance was truncated (partial measurement)")
    need(_float_or(vr.get("score"), -1.0) >= _float_or(vr.get("threshold"), 1.0),
         "verify_relevance score is below its threshold")
    # (2) The FAIL must be reviewer-only: any input/scope finding is a different
    # class and is never cleared here. And there must be something left unmeasured.
    need(_int(counts.get("input")) == 0, "the gate raised input/scope findings, not reviewer-only")
    need(_int(counts.get("code_high")) > 0, "no code_high finding to triage")
    need(not payload.get("not_checked"), "the gate left checks unmeasured (not_checked)")
    need(_int(lb.get("dirty")) == 0, "the launch baseline was dirty")
    # (3) The evidence this decision is derived from must actually be readable.
    need(bool(str(task_text or "").strip()), "no TASK text to check the findings against")
    need(bool(str(diff_text or "").strip()), "no diff available to classify the findings")
    quoted = [f for f in (findings or [])
              if str(f.get("severity", "")).lower() == "high" and _triage_norm(f.get("quote"))]
    need(bool(quoted), "no structured reviewer finding carrying a verbatim quote")
    # Every finding the GATE counted must be accounted for by a quoted one, or a
    # finding could be cleared without ever being examined.
    need(len(quoted) >= _int(counts.get("code_high")),
         "fewer quoted high findings than the gate counted -- cannot account for every finding")
    if hard:
        # `supersessions` is present on EVERY return so a consumer can read it
        # unconditionally; prerequisites failed before any of it could be computed.
        return {"action": "park", "class": "prerequisites-unmet",
                "reasons": hard[:6], "citations": [], "supersessions": []}

    tnorm = _triage_norm(task_text)
    removed, added = _triage_diff_lines(diff_text)
    rem_join, add_join = " ".join(removed), " ".join(added)
    regions = _triage_signature_rewrites(diff_text)

    def _supersession(q):
        return _triage_contract_supersession(q, regions, tnorm, task_text, add_join)

    # STAGE 4 ANNOTATION, over EVERY finding -- including the mediums the gate did
    # not escalate on. A job can park on one genuine high while a second, lower
    # finding is pure contract-supersession noise; saying so in the log is the whole
    # point (the human reads a verdict instead of re-deriving it). Advisory only.
    sup_all = []
    for fin in (findings or []):
        q = _triage_norm(fin.get("quote"))
        if not q or q not in rem_join or q in add_join:
            continue
        s = _supersession(q)
        if s:
            sup_all.append(dict(s, quote=q[:180], source=fin.get("source"),
                                severity=str(fin.get("severity", "")).lower()))
    # Every flagged FILE must be one the TASK names (an out-of-scope file is never
    # an intended removal, whatever the diff says).
    files = [str(i.get("file") or "") for i in (payload.get("issues") or [])
             if str(i.get("severity", "")).lower() == "high"]
    blockers, cits = [], []
    for f in files:
        if not f:
            blockers.append("a high finding has no file attributed -- cannot scope-check it")
        elif f not in task_text and Path(f).name not in task_text:
            blockers.append(f"flagged file {f} is not named by TASK.md (out of declared scope)")
    for fin in quoted:
        q = _triage_norm(fin.get("quote"))
        in_removed = q in rem_join
        in_added = (q in add_join) or any(q in a for a in added)
        run = _longest_common_run(q, tnorm)
        sup = _supersession(q) if (in_removed and not in_added) else None
        cit = {"quote": q[:180], "removed": in_removed, "readded": in_added,
               "contract_cite_chars": run, "cites_contract": run >= min_cite,
               "superseded": bool(sup and sup.get("ok")), "supersession": sup}
        cits.append(cit)
        # A clean removal that stage 4 proves was superseded by a TASK-dictated
        # signature rewrite is NOT a stage-3 citation failure -- so it raises no
        # blocker here. It cannot reach auto-resolve either: the supersession return
        # below intercepts first, unconditionally, and parks.
        if cit["superseded"]:
            continue
        if not in_removed:
            blockers.append(
                f"flagged literal is NOT a line this diff removed -- it is live/added code: {q[:70]!r}")
        elif in_added:
            blockers.append(
                f"flagged literal was removed AND re-added -- not a clean removal: {q[:70]!r}")
        elif not cit["cites_contract"]:
            blockers.append(
                f"TASK.md does not quote the removed literal (longest cited run {run} < "
                f"{min_cite} chars) -- removal is not provably contract-mandated: {q[:70]!r}")
    if blockers:
        return {"action": "park", "class": "not-a-proven-intended-removal",
                "reasons": blockers[:6], "citations": cits, "supersessions": sup_all}
    sup_hits = [c["supersession"] for c in cits if c.get("superseded")]
    if sup_hits:
        # STAGE 4 -- SHADOW BY CONSTRUCTION. Never "auto-resolve", in any mode. An
        # exact whole-line TASK citation of the REPLACEMENT signature is strong
        # evidence the flagged drop was ordered, but it is not stage 3's byte-exact
        # citation OF THE REMOVED LINE: a model can honour the new signature and
        # still lose an unrelated guard in the same body. So this explains, and a
        # human still decides. See the STAGE 4 note at ESCALATION_TRIAGE_MIN_SIG.
        return {
            "action": "park", "class": "contract-supersession-shadow",
            "citations": cits, "supersessions": sup_all,
            "reasons": [
                f"SHADOW-ONLY (never auto-resolves): {len(sup_hits)} of {len(quoted)} "
                f"reviewer code_high finding(s) look like CONTRACT SUPERSESSION, not a "
                f"regression -- likely reviewer false positive, but a human decides"] + [
                f"{s['where']}: TASK.md dictates the replacement signature verbatim "
                f"(`{s['new_signature'][:90]}`); dropped token(s) "
                f"{', '.join(s['dropped']) or '(none)'} appear nowhere in TASK.md's "
                f"'must NOT change' list" for s in sup_hits[:3]] + [
                f"verify is green, proven-red at baseline and relevance-proven "
                f"({_int(vr.get('killed'))} mutants killed, 0 survived)"]}
    return {
        "action": "auto-resolve", "class": "intended-removal-proven",
        "citations": cits, "supersessions": sup_all,
        "reasons": [
            f"all {len(quoted)} reviewer code_high finding(s) quote code this diff REMOVED "
            f"and never re-added, in file(s) TASK.md names",
            f"TASK.md itself quotes each removed literal (>= {min_cite} contiguous chars), so "
            f"the 'dropped' behaviour is the OLD contract the task required changing",
            f"verify is green, proven-red at baseline, and relevance-proven "
            f"({_int(vr.get('killed'))} mutants killed, 0 survived)"]}


def _float_or(v, d):
    try:
        return float(v)
    except Exception:
        return d


def _triage_evidence(job_id: str, payload: dict, gate_json: Path) -> tuple:
    """(task_text, diff_text, findings) for one job, read off disk. Fail-open to
    empty -- missing evidence is a PARK, never a clearance."""
    out = gate_json.parent
    task_text = ""
    for cand in (_job_field(job_id, "task_file"), payload.get("task_file")):
        try:
            if cand and Path(str(cand)).expanduser().is_file():
                task_text = Path(str(cand)).expanduser().read_text(errors="replace")
                break
        except Exception:
            pass
    if not task_text:
        # Fallback: the reviewer's own task.json intent (TRUNCATED to 1500 chars by
        # the pre-gate) -- enough to park on, rarely enough to clear on.
        for d in (payload.get("review_dir"), str(out / f"{job_id}-review")):
            try:
                t = json.loads((Path(str(d)) / "task.json").read_text()).get("intent") or ""
                if t:
                    task_text = t
                    break
            except Exception:
                pass
    diff_text = ""
    try:
        p = out / f"{job_id}.diff"
        if p.is_file():
            diff_text = p.read_text(errors="replace")
    except Exception:
        pass
    findings, seen = [], set()
    for d in (out / f"{job_id}-regate", out / f"{job_id}-review"):
        try:
            for fin in (json.loads((d / "run.json").read_text()).get("findings") or []):
                key = _triage_norm(fin.get("quote"))
                if key and key not in seen:
                    seen.add(key)
                    findings.append(fin)
        except Exception:
            pass
    return task_text, diff_text, findings


def _triage_log(out_dir: Path, job_id: str, payload: dict) -> None:
    """LOUD, durable audit row for every triage decision, shadow or live, next to
    AUTO-FIX-QUEUE.md. An auto-resolve must NEVER be silent."""
    try:
        dec = payload.get("escalation_triage") or {}
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        row = (f"- `{job_id}` **{dec.get('action')}** ({dec.get('class')}) "
               f"mode={payload.get('escalation_triage_mode')} "
               f"label={payload.get('job_label')} - {stamp}\n")
        for r in (dec.get("reasons") or [])[:6]:
            row += f"    - {r}\n"
        for c in (dec.get("citations") or [])[:6]:
            row += (f"    - quote removed={c.get('removed')} readded={c.get('readded')} "
                    f"cited={c.get('contract_cite_chars')}ch"
                    f"{' SUPERSEDED' if c.get('superseded') else ''}: "
                    f"`{str(c.get('quote'))[:110]}`\n")
        # STAGE 4: name every contract-superseded finding, incl. non-high ones, so a
        # human reads the verdict instead of re-deriving it from TASK.md + the diff.
        for s in (dec.get("supersessions") or [])[:6]:
            row += (f"    - {'SUPERSEDED' if s.get('ok') else 'supersession REFUSED'} "
                    f"[{s.get('severity')}/{s.get('source')}] {s.get('where')}; "
                    f"dropped={','.join(s.get('dropped') or []) or '-'}"
                    + (f"; contract PRESERVES {','.join(s.get('preserved_by_contract') or [])}"
                       if not s.get("ok") else "")
                    + f": `{str(s.get('quote'))[:90]}`\n")
        if payload.get("escalation_triage_cmd"):
            row += "    - resolve: `" + " ".join(payload["escalation_triage_cmd"]) + "`\n"
        if payload.get("escalation_triage_status"):
            row += f"    - RESULT: {payload['escalation_triage_status']}\n"
        with (out_dir / "ESCALATION-TRIAGE.md").open("a") as fh:
            fh.write(row)
    except Exception:
        pass


def escalation_triage_consider(job_id: str, payload: dict, gate_json: Path,
                               *, run=None) -> None:
    """Driver: the moment a job parks needs_opus in the reviewer-code-high class,
    decide whether it is the proven intended-removal false FAIL, ATTACH the analysis
    to the gate payload + ESCALATION-TRIAGE.md, and (LIVE only, exactly once) clear
    the park. Advisory: never raises, never changes the exit code. SHADOW (default)
    resolves nothing. Idempotent via a durable payload marker."""
    try:
        if ESCALATION_TRIAGE_MODE not in ("shadow", "live"):
            return
        if payload.get("escalation_triage_done"):
            return
        if str(payload.get("auto_fix_class") or "") != "reviewer-code-high":
            return
        task_text, diff_text, findings = _triage_evidence(job_id, payload, gate_json)
        dec = classify_escalation_triage(payload, findings, task_text, diff_text)
        if dec["action"] == "skip":
            return
        payload["escalation_triage"] = dec
        payload["escalation_triage_mode"] = ESCALATION_TRIAGE_MODE
        payload["escalation_triage_done"] = True
        argv = None
        if dec["action"] == "auto-resolve":
            argv = ["python3", str(BIN / "ollama-queue.py"), "resolve", job_id]
            payload["escalation_triage_cmd"] = argv
        try:
            gate_json.write_text(json.dumps(payload, indent=1))
        except Exception:
            pass
        print(f"[triage] {job_id} {dec['action']} ({dec['class']}) "
              f"mode={ESCALATION_TRIAGE_MODE} - {(dec.get('reasons') or [''])[0]}")
        # LIVE, once. A park is only cleared when the escalate actually happened.
        if (ESCALATION_TRIAGE_MODE == "live" and not TEST_MODE and argv is not None
                and payload.get("auto_fix_escalate_status") == "parked-needs_opus"
                and not payload.get("escalation_triage_resolved")):
            try:
                e = (run(argv) if run is not None
                     else subprocess.run(argv, capture_output=True, text=True, timeout=120))
                payload["escalation_triage_resolved"] = e.returncode == 0
                payload["escalation_triage_status"] = (
                    "resolved-false-positive" if e.returncode == 0 else "resolve-failed")
                if e.returncode != 0:
                    payload["escalation_triage_error"] = (
                        (e.stderr or e.stdout or "")[-200:])
            except Exception as ex:
                payload["escalation_triage_status"] = "resolve-failed"
                payload["escalation_triage_error"] = f"{type(ex).__name__}: {str(ex)[:160]}"
            try:
                gate_json.write_text(json.dumps(payload, indent=1))
            except Exception:
                pass
        _triage_log(gate_json.parent, job_id, payload)
    except Exception as e:
        try:
            payload["escalation_triage_error"] = f"{type(e).__name__}: {str(e)[:180]}"
            gate_json.write_text(json.dumps(payload, indent=1))
        except Exception:
            pass


# ==============================================================================
# END-TO-END AUTO-PIPELINE actions (Task C). IMPURE: git/subprocess/file copy.
# Every action is IDEMPOTENT (a durable per-job marker in the gate payload) and
# HALTS + SURFACES on any failure (parks the job needs_opus with the reason).
# They run ONLY in AUTO_PIPELINE_MODE == "live"; shadow records the decision only.
# ==============================================================================
def _origin_repo_name(cwd) -> str | None:
    """The ORIGIN repo basename for a (worktree) cwd, resolved via git's common
    dir -- NOT the worktree's own name (an auto-* worktree is wt[-slice]-<label>).
    e.g. .../resell-tracker/.git -> 'resell-tracker'. None if it cannot be resolved
    (fail-safe: apply_policy then returns 'ready', never auto-deploys)."""
    try:
        r = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--git-common-dir"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return None
        common = Path(r.stdout.strip())
        if not common.is_absolute():
            common = (Path(cwd) / common).resolve()
        # common dir is <repo>/.git (or a worktrees/.. path); its parent is the repo.
        repo = common.parent
        return repo.name or None
    except Exception:
        return None


def _worktree_toplevel(cwd) -> Path | None:
    """The worktree ROOT for a (possibly --subdir) cwd. git diff headers are
    repo-toplevel-relative, so the copy SOURCE base must be the toplevel, never the
    cwd -- otherwise a --subdir job would read targets from the wrong place. None if
    it cannot be resolved."""
    try:
        r = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=30)
        return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
    except Exception:
        return None


def _target_dirty_in_main(repo_root, rel) -> bool:
    """Does path `rel` have UNCOMMITTED changes in the origin main working tree?
    True -> auto-apply must PARK rather than clobber a human's in-progress edit.
    Fail-CLOSED: any error (git missing/odd) reads as dirty, so we never overwrite
    on an uncertain answer."""
    try:
        r = subprocess.run(["git", "-C", str(repo_root), "status", "--porcelain", "--", str(rel)],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return True
        return bool(r.stdout.strip())
    except Exception:
        return True


def _coding_dispatch_params(job_id: str, payload: dict) -> dict:
    """Model/host for the coding job an authoring job advances into. The authoring
    job's own model is the right default (it was chosen for this task); env/defaults
    fill gaps. Never raises."""
    model = str(_job_field(job_id, "model") or job_facts(job_id).get("job_model")
                or payload.get("model") or "qwen3.8:27b-q4_K_M")
    host = str(_job_field(job_id, "host_pref") or payload.get("host_pref") or "auto")
    return {"model": model, "host": host}


def _park_pipeline_failure(job_id: str, payload: dict, gate_json: Path,
                           stage: str, reason: str) -> None:
    """HALT + SURFACE: an auto-pipeline action failed -> park the job needs_opus with
    the full reason, and record it on the payload. Reuses the escalate machinery so
    the parked row is dashboard-visible in the same lane as every other escalation."""
    payload["auto_pipeline_status"] = f"{stage}-failed"
    payload["auto_pipeline_error"] = reason[:300]
    try:
        gate_json.write_text(json.dumps(payload, indent=1))
    except Exception:
        pass
    if AUTO_PIPELINE_MODE != "live" or TEST_MODE:
        return
    try:
        esc = autofix_build_escalate(job_id, autofix_root(job_id, payload),
                                     f"auto-pipeline {stage} failed: {reason}"[:400],
                                     "other", gate_json)
        subprocess.run(esc, capture_output=True, text=True, timeout=120)
    except Exception:
        pass


def advance_to_coding(job_id: str, payload: dict, gate_json: Path) -> None:
    """STAGE 3 action. The old auto-author -> draft --confirm -> coding pipeline is
    retired (use mini-swe): park with a clear message instead of sealing/enqueuing."""
    if payload.get("auto_pipeline_advanced"):
        return
    return _park_pipeline_failure(job_id, payload, gate_json, "advance",
                                  "old auto-author pipeline retired -- use mini-swe "
                                  "(mini-spec/mini-submit)")


def _apply_target_files(payload: dict, job_id: str) -> list:
    """The TARGET code files a coding job changed, from the gate's changed-file list
    minus scaffold/harness/self-test files -- NEVER the verify/fixture/check. The
    apply step copies exactly these to main."""
    changed = payload.get("changed_files") or []
    if not changed:
        # fall back to the durable diff sidecar's +++ paths
        try:
            dp = STATE_DIR / "ollama-queue-logs" / f"{job_id}.diff"
            if dp.is_file():
                changed = [m.group(1).strip() for m in
                           re.finditer(r"^\+\+\+ b/(.+)$", dp.read_text(errors='replace'), re.M)
                           if m.group(1).strip() != "/dev/null"]
        except Exception:
            changed = []
    out = []
    for p in changed:
        base = str(p).rsplit("/", 1)[-1]
        if base in _SCAFFOLD_BASENAMES:
            continue
        out.append(p)
    return out


_GATE_IDENTITY_MOD = None


def _gate_identity():
    """gate_identity.py (shared with the slicer + gate-audit). Loaded lazily; a
    missing/broken module returns None and every caller fails CLOSED."""
    global _GATE_IDENTITY_MOD
    if _GATE_IDENTITY_MOD is None:
        try:
            _p = Path(__file__).resolve().with_name("gate_identity.py")
            spec = importlib.util.spec_from_file_location("gate_identity", _p)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            _GATE_IDENTITY_MOD = m
        except Exception:
            _GATE_IDENTITY_MOD = False
    return _GATE_IDENTITY_MOD or None


def _verdict_identity_check(payload: dict, cwd) -> tuple:
    gi = _gate_identity()
    if gi is None:
        return False, ["gate_identity.py unavailable -- cannot prove the verdict "
                       "describes the code on disk (fail closed)"]
    try:
        return gi.identity_check(payload, cwd)
    except Exception as e:
        return False, [f"identity check raised {type(e).__name__}: {str(e)[:120]}"]


def stamp_judged_identity(payload: dict, job_id: str, cwd, diff_path, base: str = "") -> None:
    """Record WHAT this verdict judged (diff sha, base, head, product-file hashes).
    Advisory to the verdict itself; never raises."""
    gi = _gate_identity()
    if gi is None:
        payload.setdefault("not_checked", []).append(
            "judged-identity stamp (gate_identity.py unavailable)")
        return
    try:
        payload["judged"] = gi.judged_identity(
            job_id, cwd, diff_path, base=base,
            product_filter=lambda ps: set(product_files(ps)))
    except Exception as e:
        payload.setdefault("not_checked", []).append(
            f"judged-identity stamp ({type(e).__name__}: {str(e)[:120]})")


def apply_fix(job_id: str, payload: dict, gate_json: Path, policy: str) -> None:
    """STAGE 4 action. A clean-passing CODING job -> land the TARGET files.
      policy 'ready'      : write a 'PASS -- ready to apply' marker; copy nothing.
      policy 'auto-apply' : COPY each target file from the worktree to the ORIGIN
                            repo's main working tree (NEVER merge the branch;
                            reference_dispatch_deliverable_uncommitted_in_worktree).
    Idempotent (auto_pipeline_applied) and halt-on-error. Deliberately does NOT
    commit/push here -- landing to main is the automatable step; the repo's own CI/
    deploy (and the operator's 'validate before commit on auto-push' rule) owns the commit."""
    if payload.get("auto_pipeline_applied"):
        return
    cwd = _job_field(job_id, "cwd") or payload.get("cwd") or job_facts(job_id).get("job_cwd")
    targets = _apply_target_files(payload, job_id)
    if policy != "auto-apply":
        payload["auto_pipeline_applied"] = True
        payload["auto_pipeline_status"] = "ready-to-apply"
        payload["auto_pipeline_apply_targets"] = targets
        try:
            gate_json.write_text(json.dumps(payload, indent=1))
        except Exception:
            pass
        print(f"[gate] {job_id} auto-pipeline: PASS -- ready to apply ({len(targets)} file(s))")
        return
    # auto-apply
    if not cwd or not Path(cwd).is_dir():
        return _park_pipeline_failure(job_id, payload, gate_json, "apply",
                                      f"worktree cwd missing/gone: {cwd!r}")
    if not targets:
        return _park_pipeline_failure(job_id, payload, gate_json, "apply",
                                      "no target files to apply (diff empty after "
                                      "excluding scaffold) -- refusing a no-op apply")
    repo_name = _origin_repo_name(cwd)
    # VERDICT IDENTITY (2026-10-06 gate audit): the files about to be copied must
    # be byte-identical to the ones the verdict judged. A worktree edited after the
    # gate (a continuation round, a hand fix, a later auto-fix round's tree) would
    # otherwise land code no gate ever saw. Fail-closed: no stamp -> no auto-apply.
    _id_ok, _id_why = _verdict_identity_check(payload, cwd)
    if AUTO_PIPELINE_MODE != "live" or TEST_MODE:
        payload["auto_pipeline_apply_preview"] = {
            "policy": policy, "repo": repo_name, "targets": targets,
            "identity_ok": _id_ok, "identity_reasons": _id_why,
            "op": "copy worktree target(s) -> origin main working tree (no branch merge)"}
        try:
            gate_json.write_text(json.dumps(payload, indent=1))
        except Exception:
            pass
        return
    if not _id_ok:
        return _park_pipeline_failure(job_id, payload, gate_json, "apply",
                                      "verdict identity: " + "; ".join(_id_why)[:300])
    try:
        gc = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--git-common-dir"],
                           capture_output=True, text=True, timeout=30)
        if gc.returncode != 0:
            return _park_pipeline_failure(job_id, payload, gate_json, "apply",
                                          "cannot resolve origin repo from worktree")
        common = Path(gc.stdout.strip())
        if not common.is_absolute():
            common = (Path(cwd) / common).resolve()
        repo_root = common.parent
        # Copy SOURCE base is the worktree TOPLEVEL, not cwd: git diff paths are
        # repo-relative, so a --subdir job's targets live under the toplevel, not
        # the subdir cwd. Refuse rather than write to a wrong path if unresolved.
        toplevel = _worktree_toplevel(cwd)
        if toplevel is None:
            return _park_pipeline_failure(job_id, payload, gate_json, "apply",
                                          "cannot resolve worktree toplevel (--subdir "
                                          "safety) -- refusing to copy to a wrong path")
        copied = []
        for rel in targets:
            src = toplevel / rel
            dst = repo_root / rel
            if not src.is_file():
                return _park_pipeline_failure(job_id, payload, gate_json, "apply",
                                              f"target {rel} not found under worktree "
                                              f"toplevel {toplevel}")
            # NEVER clobber a human's in-progress edit in main: if the destination
            # path is dirty there, PARK for a human instead of overwriting.
            if _target_dirty_in_main(repo_root, rel):
                return _park_pipeline_failure(
                    job_id, payload, gate_json, "apply",
                    f"target {rel} has uncommitted local changes in main "
                    f"({repo_root}) -- refusing to overwrite; land it by hand")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            copied.append(rel)
        payload["auto_pipeline_applied"] = True
        payload["auto_pipeline_status"] = "applied-to-main"
        payload["auto_pipeline_apply_targets"] = copied
        payload["auto_pipeline_apply_repo"] = str(repo_root)
        gate_json.write_text(json.dumps(payload, indent=1))
        print(f"[gate] {job_id} auto-pipeline: APPLIED {len(copied)} file(s) to "
              f"{repo_root} (no branch merge)")
    except Exception as ex:
        return _park_pipeline_failure(job_id, payload, gate_json, "apply",
                                      f"{type(ex).__name__}: {str(ex)[:180]}")


# ==============================================================================
# AUTO-LAND (operator 2026-10-09). A SEPARATE step from AUTO_PIPELINE_MODE: shadow mode
# keeps recording 'ready-to-apply' and never lands anything; this lands a coding
# job's code-only diff on the origin repo's default branch ONLY when every one of
# these holds, else it does NOTHING and the job stays ready-to-apply (never partial):
#   1. the gate verdict is a clean PASS (no concerns / code findings / untrusted)
#   2. the cross-family second opinion has finished and AGREES
#   3. the deliverable is a code-only diff (ollama-dispatch-integrate strips and
#      verifies no scaffold file; staged files are a subset of the judged targets)
#   4. the origin repo HEAD still matches the chain's baseline (integrate classifies
#      the branch `clean`, merge-base == main tip, 0 behind, default branch checked out)
#   5. the repo's validate command passes on the staged tree BEFORE anything is
#      moved or pushed (fail-closed: no known validate command -> no landing)
# Landing is `ollama-dispatch-integrate --stage` (scratch integrate/* branch) then a
# fast-forward-only advance of the default branch to that branch -- NEVER a merge of
# the worktree branch. A failed push rewinds the default branch. Opt-out: AUTO_LAND=0.
# Every decision is printed, recorded on the gate payload (auto_land) and appended
# to AUTO-FIX-QUEUE.md. Evaluated after the gate verdict, after the second opinion
# lands, and on the daemon sweep (all idempotent).
# ==============================================================================
AUTO_LAND_VALIDATE_TIMEOUT_S = int(os.environ.get("AUTO_LAND_VALIDATE_TIMEOUT_S", "900"))
# repo basename -> validate command, when the repo carries no .dispatch-validate file.
AUTO_LAND_VALIDATE_DEFAULTS = {
    "resell-tracker": "npx prisma generate && npx tsc --noEmit && npm test",
    "local-llm-dispatch": "./run-tests.sh",
}


def _auto_land_enabled() -> bool:
    """AUTO_LAND=0 (or off/false/no) opts out; read per call so it can flip live."""
    return os.environ.get("AUTO_LAND", "1").strip().lower() not in ("0", "off", "false", "no")


def auto_land_gate_reasons(payload: dict) -> list:
    """PURE. Conditions 1+2: why this record may NOT auto-land yet ([] = both hold).
    A missing key counts against landing (fail-closed)."""
    why = []
    if str(payload.get("auto_pipeline_action")) != "apply":
        why.append(f"not an apply-stage coding PASS (auto_pipeline_action="
                   f"{payload.get('auto_pipeline_action')!r})")
    if not _gate_is_clean(payload):
        why.append(f"gate verdict {payload.get('verdict')!r} is not a clean PASS")
    if payload.get("regate") == "pending":
        why.append("authoritative re-gate still pending")
    so = payload.get("second_opinion") or {}
    ag = payload.get("second_opinion_agreement")
    if payload.get("second_opinion_disagreement") or ag == "disagree":
        why.append("second opinion DISAGREES")
    elif ag != "agree":
        why.append(f"second opinion has not agreed (review={so.get('review')!r}, "
                   f"agreement={ag!r})")
    return why


def auto_land_stage_reasons(cls: dict, stage: dict, targets: list) -> list:
    """PURE. Conditions 3+4 from integrate's classify + --stage JSON."""
    why = []
    if not isinstance(cls, dict) or cls.get("error"):
        return [f"integrate classify failed: {(cls or {}).get('error') if isinstance(cls, dict) else cls}"]
    if cls.get("overall") != "clean":
        why.append(f"integrate verdict is {cls.get('overall')!r}, not 'clean'")
    if cls.get("scope_inferred"):
        why.append("deliverable scope is inferred, not a recorded contract")
    mb, tip = str(cls.get("merge_base") or ""), str(cls.get("main_tip") or "")
    if not mb or not tip or mb != tip or int(cls.get("behind") or 0) != 0:
        why.append(f"target HEAD moved off the chain baseline (merge-base {mb[:12] or '?'}, "
                   f"main {tip[:12] or '?'}, behind {cls.get('behind')})")
    if cls.get("dirty_scope_files") or cls.get("dirty_out_of_scope_files"):
        why.append("worktree has uncommitted changes")
    if not isinstance(stage, dict) or not stage.get("staged"):
        why.append(f"stage refused: {stage if not isinstance(stage, dict) else stage.get('refuse')}")
        return why
    files = list(stage.get("diff_files") or [])
    if not files:
        why.append("staged diff is empty")
    leaked = [f for f in files if f.rsplit("/", 1)[-1] in _SCAFFOLD_BASENAMES]
    if leaked or stage.get("scaffold_leaked"):
        why.append(f"scaffold file(s) in the deliverable diff: "
                   f"{sorted(set(leaked) | set(stage.get('scaffold_leaked') or []))}")
    extra = sorted(set(files) - set(targets or []))
    if extra:
        why.append(f"staged file(s) the gate never judged: {extra}")
    return why


def auto_land_validate_cmd(repo_root) -> str | None:
    """The repo's validate command: <repo>/.dispatch-validate (first non-comment line),
    else the built-in table, else None (=> no landing; fail-closed)."""
    try:
        f = Path(repo_root) / ".dispatch-validate"
        if f.is_file():
            for ln in f.read_text().splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    return ln
    except Exception:
        pass
    # `<repo>.nosync` is the same repo (iCloud-exclusion dir name): resell-tracker.nosync held every
    # auto-land with "no validate command known" (cardBonuses-v2 964c4a833b82, 2026-10-15).
    _name = Path(repo_root).name
    if _name.endswith(".nosync"):
        _name = _name[: -len(".nosync")]
    return AUTO_LAND_VALIDATE_DEFAULTS.get(_name)


def _al_git(repo, *args, timeout=120):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, timeout=timeout)


def _al_json(cmd):
    """Run an integrate command that prints JSON; (rc, parsed-or-text)."""
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    out = r.stdout or ""
    try:
        return r.returncode, json.loads(out[out.index("{"):])
    except Exception:
        return r.returncode, (out + r.stderr)[-300:]


def _auto_land_record(job_id: str, payload: dict, gate_json: Path, status: str,
                      reasons: list, out_dir: Path | None = None, **extra) -> None:
    """Record + log ONE decision. Quiet when nothing changed since the last
    evaluation (the sweep re-evaluates every tick)."""
    prev = payload.get("auto_land") or {}
    rec = {"status": status, "reasons": [str(r)[:240] for r in reasons][:8], **extra}
    sig = (status, tuple(rec["reasons"]))
    payload["auto_land"] = {**rec, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        gate_json.write_text(json.dumps(payload, indent=1))
    except Exception:
        pass
    if sig == (prev.get("status"), tuple(prev.get("reasons") or ())):
        return
    print(f"[gate] {job_id} auto-land {status}" + (": " + "; ".join(rec["reasons"][:3]) if reasons else ""))
    try:
        od = out_dir or gate_json.parent
        row = f"- `{job_id}` **auto-land {status}** - {payload['auto_land']['at']}\n"
        for r in rec["reasons"][:4]:
            row += f"    - {r}\n"
        with (od / "AUTO-FIX-QUEUE.md").open("a") as fh:
            fh.write(row)
    except Exception:
        pass


def auto_land_consider(job_id: str, payload: dict, gate_json: Path) -> None:
    """Evaluate (and, if every condition holds, perform) the auto-land. Advisory:
    never raises, never changes the verdict or exit code. See the block comment."""
    try:
        if payload.get("auto_land", {}).get("status") == "landed":
            return
        if not _auto_land_enabled():
            if payload.get("auto_pipeline_action") == "apply":
                _auto_land_record(job_id, payload, gate_json, "disabled", ["AUTO_LAND=0"])
            return
        if str(payload.get("auto_pipeline_action")) != "apply":
            return                       # not a coding apply-stage record: nothing to say
        why = auto_land_gate_reasons(payload)
        if why:
            return _auto_land_record(job_id, payload, gate_json, "held", why)
        if TEST_MODE:
            return _auto_land_record(job_id, payload, gate_json, "held", ["GATE_TEST_MODE"])
        cwd = _job_field(job_id, "cwd") or payload.get("cwd") or job_facts(job_id).get("job_cwd")
        if not cwd or not Path(cwd).is_dir():
            return _auto_land_record(job_id, payload, gate_json, "held",
                                     [f"worktree cwd missing/gone: {cwd!r}"])
        ok, idw = _verdict_identity_check(payload, cwd)
        if not ok:
            return _auto_land_record(job_id, payload, gate_json, "held",
                                     ["verdict identity: " + "; ".join(idw)[:200]])
        targets = _apply_target_files(payload, job_id)
        if not targets:
            return _auto_land_record(job_id, payload, gate_json, "held", ["no target files"])
        repo_name = _origin_repo_name(cwd)
        gc = _al_git(cwd, "rev-parse", "--git-common-dir")
        common = Path(gc.stdout.strip()) if gc.returncode == 0 else None
        if common is None:
            return _auto_land_record(job_id, payload, gate_json, "held", ["cannot resolve origin repo"])
        if not common.is_absolute():
            common = (Path(cwd) / common).resolve()
        repo_root = common.parent
        vcmd = auto_land_validate_cmd(repo_root)
        if not vcmd:
            return _auto_land_record(job_id, payload, gate_json, "held",
                                     [f"no validate command known for {repo_name} "
                                      f"(add <repo>/.dispatch-validate)"])
        return _auto_land_record(job_id, payload, gate_json, "held",
                                 ["auto-land retired with the old integrate tool -- use mini-swe"])
    except Exception as e:
        try:
            _auto_land_record(job_id, payload, gate_json, "error",
                              [f"{type(e).__name__}: {str(e)[:180]}"])
        except Exception:
            pass


# ==============================================================================
# SLICE AUTO-FEED (operator 2026-09-18: "auto needs to auto ... one job creates the
# next"). A slice plan used to advance only when a human re-ran
# `ollama-dispatch-slice <plan> --execute`: the converged coding job sat done, its
# target never landed on the chain, and the NEXT slice never started. This is the
# EVENT that replaces that human -- gate-on-complete already runs on every job
# completion, so a completed slice job fires its plan's own advance (detached, one
# at a time, under the slicer's per-plan lock).
#
# The decision is PURE and unit-tested; the driver is a single fire-and-forget
# Popen that can never raise into or change the gate verdict.
# ==============================================================================
SLICE_RUNS = Path(os.path.expanduser("~/.ollama-dispatch/slice-runs"))
# Kill switch: GATE_SLICE_AUTOFEED=off stops the pipeline self-driving without a
# code change (leaves the manual --execute exactly as it was).
SLICE_AUTOFEED = os.environ.get("GATE_SLICE_AUTOFEED", "on").lower() != "off"
# Verdicts that may advance a chain. Deliberately NARROW: a concerns/fail verdict
# must NOT auto-commit its target onto the chain branch -- that is the needs-eyes
# lane's business.
#
# WHO ENFORCES THAT (corrected 2026-09-23). This tuple alone never did: the
# 'redispatch' set below fires the SAME --advance-detached on fail/concerns, and
# until 2026-09-23 the slicer's ENQUEUED poll landed a coding job the moment the
# QUEUE said `done`, never reading the verdict -- so 13 of the 124 gated slice
# coding jobs (5 fail, 8 concerns) were committed onto their chains. The slicer now
# routes on <id>.gate.json itself (ollama-dispatch-slice.coding_land_route): fail/
# error/nonconvergence -> the slice is ESCALATED with the findings and halts the
# chain (--accept-slice is the lever); pass/skipped/concerns -> a LIVE verify.sh
# re-run, then land, with the verdict recorded on the slice row. Keep these two
# tuples and that routing in agreement.
SLICE_ADVANCE_VERDICTS = ("pass", "skipped")

# Genuinely-RED terminal verdicts that mean the slice did NOT land and must be
# RE-DISPATCHED (Cause-2b self-recovery, 2026-09-21). Firing --advance-detached on
# these re-enters the slicer's execute(), whose FAILED branch either AUTO-LANDS a
# verify-green slice, RE-AUTHORS under the existing bounded budget, or -- once that
# budget is spent -- ESCALATES to needs_opus. Before this, nothing re-triggered
# execute() on a RED completion, so a terminally-`failed` slice sat dead forever.
# `pass-pending-review` is deliberately EXCLUDED: it is an intermediate awaiting-
# relevance-review state, not a failure, and stays a plain 'skip' as before.
SLICE_REDISPATCH_VERDICTS = ("fail", "error", "nonconvergence", "concerns")


def _slice_base_label(label) -> str:
    """PURE. The slice's OWN label, with the authoring wrapper stripped:
    'auto-author-alpha-s2' -> 'alpha-s2', 'auto-refine-alpha-s2-r3' -> 'alpha-s2',
    'alpha-s2' -> 'alpha-s2'. Mirrors advance_to_coding's base-label derivation.

    GATE AUTO-FIX MARKER (2026-10-06, 01fdda651fba 'rt-egift-link-s1-s0-db-schema
    [auto-fix r1]', b0c6d90fdf38 'sidecar-bfmr-sink-s1-route [auto-fix r1]'): the
    gate's own requeue appends ' [auto-fix rN]' to the label (build_autofix_requeue).
    It was never stripped, so every auto-fix round of a slice job resolved to
    "does not belong to any slice run" and its completion never fired the owning
    plan's advance -- the slicer follows the round (autofix_successor) but only a
    later unrelated trigger landed it. Strip the marker FIRST."""
    s = re.sub(r"\s*\[auto-fix r\d+\]\s*$", "", str(label or ""))
    s = re.sub(r"^(?:auto-author|auto-refine)-", "", s)
    return re.sub(r"-r\d+$", "", s)


def slice_run_for_label(label, runs) -> dict | None:
    """PURE. The slice run (a loaded run-state dict) that owns this job label, or
    None. `runs` is a list of run-state dicts. A label belongs to a run when it is
    '<run label>-<slice id>' for a slice id the run actually declares -- matching on
    the run label alone would claim any job that merely shares a prefix.

    LONGEST run-label match wins, so a plan named 'alpha' cannot steal a job that
    belongs to a plan named 'alpha-extra'."""
    base = _slice_base_label(label)
    if not base:
        return None
    best = None
    for run in runs or []:
        rl = str((run or {}).get("label") or "")
        if not rl or not base.startswith(rl + "-"):
            continue
        sid = base[len(rl) + 1:]
        if sid not in (run.get("slices") or {}):
            continue
        if best is None or len(rl) > len(str(best.get("label") or "")):
            best = run
    return best


def slice_autofeed_decision(label, verdict, rec, runs) -> tuple:
    """PURE. (action, reason, plan_path) for a completed job:
      'advance'    -- a slice job of a known plan that should LAND / progress;
      'redispatch' -- a slice job of a known plan that FAILED and must be
                      re-dispatched (bounded retry -> needs_opus) so it never sits
                      dead (Cause-2b self-recovery);
      'skip'       -- not a slice job we own, or an intermediate state to leave be.

    Both 'advance' and 'redispatch' fire the SAME `--advance-detached`; the slicer's
    execute() is STATUS-driven and decides land / auto-retry / escalate from the
    slice's on-disk state AND (2026-09-23) from this job's own .gate.json verdict --
    a queue-`done` coding job with a fail verdict is ESCALATED there, not landed.
    Splitting them here is for a legible decision log and so the trigger never fires
    on a job that is not ours.

    FAIL-CLOSED: only a positively-recognised slice job acts. RESEARCH/--runner jobs
    (bonsai) are excluded first and explicitly -- they are ungateable by construction
    (_is_ungateable_job) and must never be auto-fed a coding job.

    CAUSE-2a AUTO-LAND (2026-09-21): an `auto-author-`/`auto-refine-` job whose OWN
    verify came back GREEN (worker exit 0 == verify.sh printed VERIFY_OK at HEAD) but
    whose terminal VERDICT was DEMOTED to `concerns` -- the confirmed dirty-launch-
    baseline INPUT demotion (bg-dashboard-s3, esim-global-s3) -- is ADVANCED, not
    parked as failed. The GREEN verify is the deliverable's own proof. This does NOT
    bypass a safety gate: the landing runs through `--advance-detached`, whose
    execute() FAILED-branch re-runs verify AND still enforces the relevance /
    DRAFT_UNCONFIRMED review before anything is committed. A GENUINE nonconvergence
    has a NON-zero exit, never satisfies this, and falls through to 'redispatch'."""
    rec = rec or {}
    if str(rec.get("task_kind") or "") == "research":
        return ("skip", "research job -- never auto-fed (no code diff)", None)
    if rec.get("runner"):
        return ("skip", "alternate --runner job -- never auto-fed (no code diff)", None)
    run = slice_run_for_label(label, runs)
    if run is None:
        return ("skip", f"{label!r} does not belong to any slice run", None)
    plan = run.get("plan_path")
    if not plan:
        return ("skip", f"slice run {run.get('label')!r} has no plan_path recorded "
                        f"(re-run --execute once to register it)", None)
    verdict_l = str(verdict or "").lower()
    if verdict_l in SLICE_ADVANCE_VERDICTS:
        return ("advance", f"slice job of plan {run.get('label')!r} at verdict "
                           f"{verdict!r} -- advancing the chain", plan)
    author_verify_passed = (_is_authoring_label(label) and rec.get("exit_code") == 0)
    if author_verify_passed and verdict_l == "concerns":
        return ("advance", f"slice job of plan {run.get('label')!r}: authoring/refine "
                           f"verify GREEN at HEAD (worker exit 0) but verdict {verdict!r} "
                           f"(the confirmed dirty-launch-baseline demotion) -- AUTO-LANDING "
                           f"the green-but-unconverged slice via --advance-detached, which "
                           f"still enforces the relevance/DRAFT_UNCONFIRMED gate", plan)
    if verdict_l in SLICE_REDISPATCH_VERDICTS:
        return ("redispatch", f"slice job of plan {run.get('label')!r} reached RED verdict "
                              f"{verdict!r} (verify not green) -- re-dispatching the owning "
                              f"plan so its bounded retry / needs_opus escalation runs "
                              f"instead of parking the slice dead", plan)
    return ("skip", f"verdict {verdict!r} is neither advanceable "
                    f"({', '.join(SLICE_ADVANCE_VERDICTS)}) nor a re-dispatchable failure "
                    f"({', '.join(SLICE_REDISPATCH_VERDICTS)}) -- intermediate state, left be",
            None)


def _load_slice_runs(root: Path = None) -> list:
    """Every slice run-state on disk. Never raises."""
    out = []
    root = root or SLICE_RUNS
    try:
        for p in sorted(root.glob("*.json")):
            try:
                d = json.loads(p.read_text())
            except Exception:
                continue
            if isinstance(d, dict) and d.get("label") and d.get("slices"):
                out.append(d)
    except Exception:
        pass
    return out


def slice_pipeline_consider(job_id: str, payload: dict, gate_json: Path) -> None:
    """Fire the owning slice plan's DETACHED advance when a slice job completes.
    Advisory: wrapped end to end, never raises, never touches the exit code. The
    slicer's own per-plan lock makes a double-fire a no-op, so this is safe to call
    from every terminal path (merge_review, emit, the catch-up sweep)."""
    try:
        label = payload.get("job_label") or _job_field(job_id, "label") or ""
        rec = _terminal_facts(job_id) or _live_job(job_id) or {}
        action, reason, plan = slice_autofeed_decision(
            label, payload.get("verdict"), rec, _load_slice_runs())
        payload["slice_autofeed"] = {"action": action, "reason": reason}
        try:
            gate_json.write_text(json.dumps(payload, indent=1))
        except Exception:
            pass
        # 'advance' LANDS/progresses a slice; 'redispatch' kicks a FAILED slice into
        # the bounded-retry/escalate path (Cause-2b). Both fire the SAME detached
        # --execute; the slicer is status-driven and does the right thing. 'skip' is
        # a job we do not own or an intermediate state -- leave it.
        if action not in ("advance", "redispatch"):
            return
        if not SLICE_AUTOFEED or TEST_MODE:
            print(f"[gate] {job_id} slice-autofeed SHADOW (would {action} {plan})")
            return
        print(f"[gate] {job_id} slice-autofeed ({action}): {reason}")
        print(f"[gate] {job_id} slice-autofeed: old slicer retired, not launched (use mini-swe)")
    except Exception as e:
        try:
            payload["slice_autofeed_error"] = f"{type(e).__name__}: {str(e)[:180]}"
            gate_json.write_text(json.dumps(payload, indent=1))
        except Exception:
            pass


def auto_pipeline_consider(job_id: str, payload: dict, gate_json: Path) -> None:
    """Success-path driver, the mirror of autofix_consider. Computes classify_advance,
    records it on the payload, and (LIVE only) performs the advance/apply exactly
    once. Advisory: never raises, never changes the exit code. OFF by default
    (AUTO_PIPELINE_MODE 'shadow')."""
    try:
        label = payload.get("job_label") or _job_field(job_id, "label")
        cwd = _job_field(job_id, "cwd") or payload.get("cwd") or job_facts(job_id).get("job_cwd")
        repo_name = _origin_repo_name(cwd) if cwd else None
        dec = classify_advance(payload, label, repo_name)
        payload["auto_pipeline_stage"] = dec["stage"]
        payload["auto_pipeline_action"] = dec["action"]
        payload["auto_pipeline_policy"] = dec.get("policy")
        payload["auto_pipeline_reasons"] = (dec.get("reasons") or [])[:4]
        if dec.get("evidence_gaps") is not None:
            payload["auto_pipeline_evidence_gaps"] = dec["evidence_gaps"][:10]
        payload["auto_pipeline_mode"] = AUTO_PIPELINE_MODE
        try:
            gate_json.write_text(json.dumps(payload, indent=1))
        except Exception:
            pass
        print(f"[gate] {job_id} auto-pipeline={dec['action']} stage={dec['stage']} "
              f"mode={AUTO_PIPELINE_MODE}")
        if dec["action"] == "advance-to-coding":
            advance_to_coding(job_id, payload, gate_json)
        elif dec["action"] == "apply":
            apply_fix(job_id, payload, gate_json, dec.get("policy") or "ready")
    except Exception as e:
        try:
            payload["auto_pipeline_error"] = f"{type(e).__name__}: {str(e)[:180]}"
            gate_json.write_text(json.dumps(payload, indent=1))
        except Exception:
            pass


# ==============================================================================
# RUN-STATUS JANITOR (daemon-level auto-archive of rows that never need a human).
# PURE decision core + PURE sibling selection (so --self-test exercises the exact
# logic the daemon runs), then a thin impure driver that talks to the queue-api.
# ==============================================================================
def _is_real_app_repo(cwd) -> bool:
    """PURE. True iff cwd is a REAL application repo checkout (under
    ~/Desktop/GitHub Projects/<repo>), NOT a dispatch worktree/scratch/bakeoff
    tree. A CONCERNS/FAIL there is a real deliverable defect and must reach a
    human; a non-pass inside a scratch/eval tree is not."""
    c = str(cwd or "")
    if not c:
        return False
    if "/.ollama-dispatch/" in c or "/scratchpad/" in c or "/bakeoff" in c:
        return False
    return any(c == r or c.startswith(r + "/") for r in _GH_PROJECTS_ROOTS)


def _is_eval_or_probe(label, cwd) -> bool:
    """PURE-ish (reads only label/cwd + handoff-emit's cached predicate). A
    measurement arm -- eval/bake-off/probe -- has no deliverable to integrate."""
    lab = str(label or "")
    hm = _handoff_mod()
    try:
        if hm and hm._label_is_eval(lab):
            return True
    except Exception:
        pass
    if _EVAL_LABEL_RE.search(lab):
        return True
    c = str(cwd or "")
    if re.search(r"/r\d*probe-", c) or "/bakeoff" in c or "/scratchpad/" in c:
        return True
    return False


def _is_dead_slice_fragment(label, status) -> bool:
    """PURE-ish (reads the slice-runs dir). A FAILED slice-fragment dispatch
    ("<feature>-s<N>-...") whose plan/run STATE FILE is gone -- the chain was
    superseded/removed, so the orphaned fragment row is dead. If the run-state
    still exists the chain owns it -> NOT dead (surface). Fail-safe: any doubt
    (unreadable dir, non-fragment label, live status) -> False (surface)."""
    if str(status) not in ("failed", "blocked", "error"):
        return False
    # BUG (2026-09-23, seen on ce6741873b5b / 036cd54d7366 auto-author-aw-sched-
    # runner-s19-run-schedule): the authoring wrapper was never stripped, so `feat`
    # came out as 'auto-author-aw-sched-runner', whose run-state file does not
    # exist -- and a slice of a LIVE chain was classified dead-slice. Shadow mode
    # hid it; live it would auto-archive real chain failures out of needs-eyes.
    m = _SLICE_FRAG_RE.match(_slice_base_label(label))
    if not m:
        return False
    try:
        return not (_SLICE_RUNS_DIR / f"{m.group('feat')}.json").exists()
    except Exception:
        return False


def classify_runstatus_janitor(*, label, cwd, verdict, status,
                               awaiting_signoff, is_dead_slice) -> dict:
    """PURE. Decide the SELF action for one finished job: 'auto-archive' (safe to
    clear without a human) or 'surface' (must reach a human). Returns
    {action, category, reason}. FAIL-SAFE: SURFACE wins on any doubt -- only two
    positively-recognised categories ever auto-archive, and every human-owed shape
    is checked FIRST so nothing that needs eyes can be archived."""
    # 1. still owes a REQUIRED sign-off -> never archive (also refused server-side)
    if awaiting_signoff:
        return {"action": "surface", "category": "awaiting-signoff",
                "reason": "PASS deliverable still needs human sign-off"}
    # 2. not terminal -> live/active (a running job or a pending next round)
    if str(status) not in _JANITOR_TERMINAL:
        return {"action": "surface", "category": "live",
                "reason": f"status {status!r} is not terminal (live/active chain)"}
    # 3. a non-pass on a REAL app repo is a real defect -> must reach a human
    if str(verdict).split(" ")[0] in ("concerns", "fail", "error") \
            and _is_real_app_repo(cwd):
        return {"action": "surface", "category": "real-repo-non-pass",
                "reason": f"verdict {verdict!r} on a real app repo -- needs eyes"}
    # 4. a measurement arm has no deliverable -> auto-archive
    if _is_eval_or_probe(label, cwd):
        return {"action": "auto-archive", "category": "eval-arm",
                "reason": "eval/bake-off/probe measurement arm -- no deliverable"}
    # 5. an orphaned dead slice fragment -> auto-archive
    if is_dead_slice:
        return {"action": "auto-archive", "category": "dead-slice",
                "reason": "failed slice fragment whose plan/run state is gone"}
    # 6. FAIL-SAFE: not a positively-safe category -> surface
    return {"action": "surface", "category": "default-safe",
            "reason": "not a positively-safe auto-archive category -> surface"}


def _feature_and_rank(label) -> tuple:
    """PURE. Map a stage label to (feature_base, stage_rank) for STAGE-DEDUP.
    An authoring stage is 'auto-author-<base>' (rank 0); a refine stage is
    'auto-refine-<base>-r<N>' (rank N); anything else (the coding/final row, whose
    label IS the bare <base>) is (label, +inf) -- the newest stage, which
    supersedes all authoring/refine rows of the same base."""
    s = str(label or "")
    m = re.match(r"^auto-(author|refine)-(.+)$", s)
    if not m:
        return s, float("inf")
    kind, rest = m.group(1), m.group(2)
    rounds = re.findall(r"-r(\d+)", rest)
    base = re.sub(r"(?:-r\d+)+$", "", rest)
    rank = 0 if kind == "author" else (int(rounds[-1]) if rounds else 1)
    return base, rank


def select_superseded_siblings(rows, current_label) -> list:
    """PURE. Given the run-status rows (each a dict with id/label/awaiting_signoff)
    and the label of the job that just reached a terminal verdict, return the
    EARLIER authoring/refine STAGE rows of the SAME feature that are now superseded
    (keep only the latest/terminal row). Never returns the current row, a different
    feature, a non-auto-stage row, an equal/newer stage, or one still awaiting
    sign-off -- all fail-safe exclusions."""
    cur_base, cur_rank = _feature_and_rank(current_label)
    out = []
    for r in rows:
        lab = str(r.get("label") or "")
        if not lab or lab == str(current_label):
            continue
        if not re.match(r"^auto-(author|refine)-", lab):
            continue                      # only intermediate authoring/refine rows
        base, rank = _feature_and_rank(lab)
        if base != cur_base or rank >= cur_rank:
            continue                      # different feature or not strictly earlier
        if r.get("awaiting_signoff"):
            continue                      # never archive a sign-off-owed row
        out.append(r)
    return out


def _janitor_runs(fetch=None) -> list:
    """The current run-status list (what the dashboard shows). Injectable `fetch`
    for tests; default GETs the queue-api. Fail-open: [] on any error (so
    stage-dedup simply does nothing rather than misfiring)."""
    if fetch is not None:
        return fetch() or []
    try:
        import urllib.request
        with urllib.request.urlopen(urllib.request.Request(QUEUE_API_URL + "/api/runs", headers=queue_config.queue_api_headers()), timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, list) else (data.get("runs") or [])
    except Exception:
        return []


def _janitor_clear(job_id, post=None) -> dict:
    """Archive one row via the SAME endpoint the dashboard 'clear' button uses:
    POST /api/runs/<id>/clear with an EMPTY body (never an override -- a job that
    still owes sign-off is refused server-side, which is the safety we want).
    Injectable `post` for tests. Returns {ok, status, ...}; fail-open."""
    if post is not None:
        return post(job_id)
    try:
        import urllib.request
        req = urllib.request.Request(
            QUEUE_API_URL + f"/api/runs/{job_id}/clear", data=b"{}",
            headers={"Content-Type": "application/json", **queue_config.queue_api_headers()}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return {"ok": True, "status": resp.status,
                    "body": resp.read().decode("utf-8")[:200]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}


def _janitor_log(job_id, dec, siblings) -> None:
    """At-a-glance worklist of janitor decisions (shadow + live). Never raises."""
    try:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        line = (f"- `{job_id}` **{dec['action']}** ({dec['category']}) "
                f"mode={RUNSTATUS_JANITOR_MODE} - {dec['reason']} - {stamp}\n")
        for s in siblings:
            line += (f"    - supersedes `{s.get('id')}` "
                     f"({s.get('label')}) -> archive\n")
        with (STATE_DIR / "ollama-queue-logs" / "RUNSTATUS-JANITOR.md").open("a") as fh:
            fh.write(line)
    except Exception:
        pass


def land_ingest_consider(job_id: str, payload: dict) -> None:
    """LAND PIPELINE hand-off (2026-10-09, ollama-land): a standalone dispatch that finished with
    a landable verdict is registered for a LANDING PACKET. Fire-and-forget and best-effort: it
    only stages a scratch integrate/* branch and writes landing/<chain>/packet.*; it never
    merges or pushes (REVIEW mode) -- `ollama-land approve` is the only writer of a product
    repo. Skipped in test mode / sandbox, and when ollama-land is not installed beside us."""
    if TEST_MODE or os.environ.get("DISPATCH_VERIFY_SANDBOX"):
        return
    if str(payload.get("verdict")) not in ("pass", "concerns"):
        return
    tool = BIN / "ollama-land"
    if not tool.is_file():
        return
    subprocess.Popen(["python3", str(tool), "ingest", "--job", job_id], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def runstatus_janitor_consider(job_id: str, payload: dict, gate_json: Path,
                               *, fetch=None, post=None) -> None:
    """Daemon-level driver: at a job's terminal completion, auto-archive it (and its
    superseded authoring/refine sibling rows) IFF safe. Advisory: never raises,
    never changes the exit code. SHADOW (default) LOGS what it would archive and
    archives nothing; LIVE POSTs the clears. Idempotent via a payload marker."""
    try:
        if RUNSTATUS_JANITOR_MODE not in ("shadow", "live"):
            return
        if payload.get("runstatus_janitor_done"):
            return
        label = (payload.get("job_label") or _job_field(job_id, "label")
                 or job_facts(job_id).get("job_label"))
        cwd = (payload.get("job_cwd") or _job_field(job_id, "cwd")
               or job_facts(job_id).get("job_cwd"))
        verdict = str(payload.get("verdict") or "")
        status = str(_job_field(job_id, "status")
                     or job_facts(job_id).get("job_status") or "done")
        hm = _handoff_mod()
        awaiting = False
        try:
            awaiting = bool(hm and hm.signoff_blocks_acting(job_id, label))
        except Exception:
            awaiting = True         # fail-safe: can't tell -> treat as owed -> surface
        is_dead = _is_dead_slice_fragment(label, status)
        dec = classify_runstatus_janitor(
            label=label, cwd=cwd, verdict=verdict, status=status,
            awaiting_signoff=awaiting, is_dead_slice=is_dead)
        siblings = select_superseded_siblings(_janitor_runs(fetch=fetch), label)
        payload["runstatus_janitor_mode"] = RUNSTATUS_JANITOR_MODE
        payload["runstatus_janitor"] = {
            "self": dec,
            "superseded": [{"id": s.get("id"), "label": s.get("label")}
                           for s in siblings]}
        payload["runstatus_janitor_done"] = True
        _janitor_log(job_id, dec, siblings)
        print(f"[janitor] {job_id} {dec['action']} ({dec['category']}) "
              f"mode={RUNSTATUS_JANITOR_MODE} superseded={len(siblings)} "
              f"- {dec['reason']}")
        try:
            gate_json.write_text(json.dumps(payload, indent=1))
        except Exception:
            pass
        if RUNSTATUS_JANITOR_MODE != "live":
            return
        # LIVE: archive self (if a safe category) then each superseded sibling.
        results = {}
        if dec["action"] == "auto-archive":
            results[job_id] = _janitor_clear(job_id, post=post)
        for s in siblings:
            sid = s.get("id")
            if sid:
                results[sid] = _janitor_clear(sid, post=post)
        if results:
            payload["runstatus_janitor_cleared"] = results
            try:
                gate_json.write_text(json.dumps(payload, indent=1))
            except Exception:
                pass
    except Exception:
        pass


def _fs_safe(s: str) -> str:
    """A filesystem-safe fragment for a drop-folder name. Collapses anything that
    is not [A-Za-z0-9._-] to '-', trims, and caps length. Never empty."""
    out = re.sub(r"[^A-Za-z0-9._-]+", "-", str(s or "").strip()).strip("-. ")
    return out[:80] or "job"


def _read_review_intent(task_file) -> tuple:
    """(task_text, not_checked_note) for the model review's `intent`. Never raises.

    The gate checks the task file exists ONCE, at its start (the `skipped` guard in
    main). Between that check and this read it runs get_diff, the decidable checks
    and the relevance measurement -- tens of seconds during which a concurrent
    slicer --advance-detached may reap the whole harness worktree (2026-09-24,
    arr-codec-floor s4 r3: r2's gate landed, its advance cleared the worktree for an
    auto-retry 9s after r3 finished, and r3's gate died 4s later on
    FileNotFoundError reading AUTO-TASK.md -- the third identical traceback in the
    daemon log). A dead gate leaves NO verdict, and everything downstream (the
    slicer's autofeed, signoff, run-status, the queue's focus) waits on silence.
    The diff is already captured in the durable sidecar, so the review can still run
    on the diff alone: degrade the intent to a note and ABSTAIN visibly (not_checked),
    which is this file's convention for "could not measure"."""
    try:
        return Path(task_file).expanduser().read_text(errors="replace"), ""
    except Exception as e:
        why = f"{type(e).__name__}: {str(e)[:160]}"
        return (f"(The dispatch TASK file {task_file} could not be read when this review "
                f"was enqueued -- {why}. The harness worktree was most likely reaped by a "
                f"concurrent slicer advance after the diff was captured. Review the diff "
                f"on its own merits: report only defects this change introduces.)",
                f"review-intent (task file {task_file} vanished between the gate's start "
                f"and the review enqueue -- {why}; the worktree was likely reaped "
                f"mid-gate, so the reviewer saw the diff but not the TASK)")


def _one_line_intent(task_file) -> str:
    """The one-line human intent for the SUMMARY, from the dispatch task file.
    JSON task files carry it under a known key; a TASK.md's first real line is it.
    Best-effort: returns '' rather than raising."""
    try:
        raw = Path(task_file).expanduser().read_text(errors="replace").strip()
    except Exception:
        return ""
    if raw[:1] == "{":
        try:
            j = json.loads(raw)
            for k in ("intent", "task", "goal", "summary", "title"):
                if j.get(k):
                    return str(j[k]).strip().splitlines()[0][:200]
        except Exception:
            pass
    for ln in raw.splitlines():
        s = ln.strip().lstrip("#").strip()
        if s:
            return s[:200]
    return ""


def _changed_files_from_diff(diff_text: str) -> list[str]:
    """The post-change repo-relative paths a unified git diff touches, from its
    '+++ b/<path>' headers. Skips /dev/null (a deletion's post-image)."""
    files: list[str] = []
    for m in re.finditer(r"^\+\+\+ b/(.+)$", diff_text, re.M):
        p = m.group(1).strip()
        if p and p != "/dev/null" and p not in files:
            files.append(p)
    return files


def drop_completed_code(job_id: str, payload: dict, out_dir: Path) -> Path | None:
    """Drop a finished dispatch's deliverable into COMPLETED_ROOT so the operator can
    eyeball what the model produced, keyed off the gate verdict:

        COMPLETED_ROOT/Pass/<label>-<shortid>/
        COMPLETED_ROOT/Failed/<label>-<shortid>/

    Each folder gets: <job_id>.diff (the actual change), files/<path> (the changed
    files' post-change contents from the worktree, diff-only when the tree is gone
    or a file is too large), and SUMMARY.md (label, id, model, host, verdict, ts,
    one-line intent).

    Reads the .diff + gate payload WHILE THEY ARE STILL IN out_dir -- this fires at
    gate time, well before the dashboard's clear/archive move
    (ollama-queue-api._archive_run), so it never fights that.

    IDEMPOTENT + SINGLE-BUCKET: re-running for the same job refreshes its one
    folder and moves it between Pass/ and Failed/ if the verdict changed (a
    pre-gate that a Studio re-gate later supersedes). Dedupe is by the stable
    -<shortid> suffix, so a label that drifts across auto-fix rounds still resolves
    to one folder. Advisory: returns the drop path, or None when there is nothing
    terminal to drop, and NEVER raises (rule 2 -- must not affect the gate)."""
    try:
        verdict = str(payload.get("verdict") or "")
        # Only a TERMINAL verdict with a real code change is worth dropping. No
        # diff => nothing to browse (a skipped research task, an errored gate).
        # pass-pending-review is non-terminal (the review re-fires this later).
        if verdict in ("", "pass-pending-review", "skipped", "error"):
            return None
        diff_src = out_dir / f"{job_id}.diff"
        if not diff_src.exists():
            return None

        bucket = "Pass" if verdict == "pass" else "Failed"
        shortid = _fs_safe(job_id)[:12] or "job"
        name = f"{_fs_safe(payload.get('job_label') or 'job')}-{shortid}"
        root = COMPLETED_ROOT

        # SINGLE-BUCKET: clear any prior drop for this job in EITHER bucket first,
        # matched by the -<shortid> suffix (label may differ across rounds/regate).
        for b in ("Pass", "Failed"):
            bdir = root / b
            if bdir.is_dir():
                for prior in bdir.glob(f"*-{shortid}"):
                    if prior.is_dir():
                        shutil.rmtree(prior, ignore_errors=True)

        dest = root / bucket / name
        dest.mkdir(parents=True, exist_ok=True)

        # 1. the diff -- the authoritative, complete record of the change.
        shutil.copyfile(diff_src, dest / f"{job_id}.diff")

        # 2. post-change contents of the changed files (diff-only where impractical).
        diff_text = diff_src.read_text(errors="replace")
        cwd = Path(str(payload.get("job_cwd") or "")).expanduser()
        file_notes: list[str] = []
        captured = 0
        if cwd.is_dir():
            for rel in _changed_files_from_diff(diff_text):
                src = cwd / rel
                try:
                    if not src.is_file():
                        file_notes.append(f"- `{rel}` -- not present post-change "
                                          f"(deleted/renamed); see the diff")
                        continue
                    sz = src.stat().st_size
                    if sz > COMPLETED_MAX_FILE_BYTES:
                        file_notes.append(f"- `{rel}` -- {sz} bytes, too large to "
                                          f"copy; see the diff")
                        continue
                    outp = dest / "files" / rel
                    outp.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, outp)
                    captured += 1
                    file_notes.append(f"- `{rel}`")
                except Exception as e:
                    file_notes.append(f"- `{rel}` -- capture failed: {type(e).__name__}")
        else:
            file_notes.append(f"- worktree `{cwd}` is gone (reaped); the diff is "
                              f"the full record")

        # 3. SUMMARY.md -- what the operator reads at a glance.
        ts = payload.get("ts") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        intent = payload.get("task_intent") or ""
        model = payload.get("job_model") or payload.get("model") or "?"
        host = payload.get("job_host") or payload.get("host") or "?"
        counts = payload.get("counts") or {}
        lines = [
            f"# {payload.get('job_label') or job_id}",
            "",
            f"- **Verdict:** {verdict}  (-> `{bucket}/`)",
            f"- **Job id:** `{job_id}`",
            f"- **Model:** {model}",
            f"- **Host:** {host}",
            f"- **Completed:** {ts}",
            f"- **Worktree:** `{cwd}`",
        ]
        if payload.get("review_verdict"):
            lines.append(f"- **Review verdict:** {payload.get('review_verdict')}")
        if counts:
            lines.append(f"- **Findings:** code_high={counts.get('code_high', 0)} "
                         f"code={counts.get('code', 0)} input={counts.get('input', 0)}")
        if payload.get("gate_authority"):
            lines.append(f"- **Gate authority:** {payload.get('gate_authority')}")
        lines += ["", "## Intent", "", (intent or "_(no task-file intent captured)_"),
                  "", "## Changed files", ""]
        lines += file_notes or ["- _(none detected in the diff)_"]
        lines += ["", "## Contents", "",
                  f"- `{job_id}.diff` -- the full unified diff (authoritative).",
                  (f"- `files/` -- post-change contents of {captured} changed file(s)."
                   if captured else
                   "- `files/` -- omitted (see the diff; worktree gone or files too large)."),
                  ""]
        (dest / "SUMMARY.md").write_text("\n".join(lines))
        print(f"[gate] {job_id} code-drop -> {dest}")
        return dest
    except Exception:
        # Rule 2: the drop is a convenience view; a failure here loses nothing and
        # must never be able to affect the gate's outcome.
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--task-file", default=None)
    ap.add_argument("--verify", default="")
    ap.add_argument("--model", default="qwen3.8:27b-q4_K_M")  # coding default flipped 2026-09-02: bake-off 6/6 vs qwen3-coder 1/6
    ap.add_argument("--host", default="http://127.0.0.1:11434")
    ap.add_argument("--num-ctx", type=int, default=32768)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--verify-exit", type=int, default=None,
                    help="override the job's verify exit code (testing). "
                         "Default: read it from the queue state.")
    ap.add_argument("--launch-dirty", type=int, default=-1,
                    help="override the queue's launch_baseline.dirty count "
                         "(testing, or a caller that already knows it). -1 = "
                         "read it from the queue state.")
    ap.add_argument("--auto-pipeline-resume", action="store_true",
                    help="idempotent CATCH-UP for the end-to-end auto-pipeline: load "
                         "this job's existing <id>.gate.json and re-run the success-path "
                         "advance/apply (guarded by its own markers). Used by the queue "
                         "daemon's per-tick sweep; does NOT re-gate. No-op unless "
                         "GATE_AUTO_PIPELINE=live.")
    ap.add_argument("--job-label", default="",
                    help="the completed job's label; a 'gate-' prefix means this "
                         "invocation merges a review result instead of gating")
    a = ap.parse_args()

    # LOOP GUARD: a gate's own review job must never be gated. When the queue
    # completes a 'gate-<id>' job this hook fires again -- that invocation MERGES
    # the review into the parent's .gate.json instead of starting a new gate.
    # 'regate-' is checked FIRST and is disjoint from 'gate-' (neither prefixes
    # the other). A regate completion folds the authoritative Studio-27B verdict
    # and is terminal -- it never escalates again.
    # 'secondop-' is likewise disjoint from both and NEVER authoritative: it folds
    # a cross-family advisory review in as a flag and can change no verdict.
    # 'secondop-<parent>-fcheck' is the GATE FINDING CHECK verifier (see _gfc): checked
    # BEFORE the plain secondop- branch, which it would otherwise be mistaken for.
    if re.match(r"^secondop-.+-fcheck$", a.job_label):
        return merge_finding_check(a, out_dir_for(a))
    if a.job_label.startswith("secondop-"):
        return merge_second_opinion(a, out_dir_for(a))
    if a.job_label.startswith("regate-"):
        return merge_review(a, out_dir_for(a), prefix="regate-", authoritative=True)
    if a.job_label.startswith("gate-"):
        return merge_review(a, out_dir_for(a))

    # NO-DIFF GUARD (defence in depth; ollama-queue._is_ungateable_job already
    # suppresses the Popen). A research job and/or a job that ran via an alternate
    # --runner has no attributable dispatch diff, so there is nothing for the CODE
    # gate to review -- and gating it spawns a gate/regate that seizes the exclusive
    # Studio gate lane and holds every authoring job behind it. Exit 0 silently
    # (with a line for the log): no gate.json, no review job, no escalation.
    _rec = _terminal_facts(a.job_id) or _live_job(a.job_id)
    if job_is_ungateable(_rec):
        print(f"[gate] {a.job_id} ({a.job_label or '?'}) NOT gated -- "
              f"task_kind={(_rec or {}).get('task_kind') or 'coding'}, "
              f"runner={'yes' if (_rec or {}).get('runner') else 'no'}: "
              f"no code diff to review")
        return 0

    # AUTO-PIPELINE CATCH-UP (Task C per-tick sweep): re-run the success-path
    # advance/apply from the ALREADY-computed gate.json. Idempotent (the actions
    # self-guard on their markers), never re-gates, and a no-op in shadow mode.
    if a.auto_pipeline_resume:
        _od = out_dir_for(a)
        _gj = _od / f"{a.job_id}.gate.json"
        if not _gj.is_file():
            return 0
        try:
            _pl = json.loads(_gj.read_text())
        except Exception:
            return 0
        if not _pl.get("job_label"):
            _pl["job_label"] = a.job_label or None
        auto_pipeline_consider(a.job_id, _pl, _gj)
        auto_land_consider(a.job_id, _pl, _gj)
        slice_pipeline_consider(a.job_id, _pl, _gj)
        # Daemon per-tick sweep also drives the run-status janitor (idempotent).
        runstatus_janitor_consider(a.job_id, _pl, _gj)
        return 0

    cwd = Path(a.cwd).expanduser()
    out_dir = Path(a.out_dir).expanduser() if a.out_dir else (STATE_DIR / "ollama-queue-logs")
    out_dir.mkdir(parents=True, exist_ok=True)
    gate_json = out_dir / f"{a.job_id}.gate.json"
    diff_path = None        # set once get_diff() succeeds; emit() checks for None

    def emit(payload: dict) -> int:
        payload["job_id"] = a.job_id
        payload["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        payload["advisory"] = True
        # Snapshot the job's own outcome INTO the gate record. The queue prunes
        # finished jobs from its state file (3 live rows against 21 completed
        # dispatches on disk), so anything reading job status later -- the
        # handoff surface especially -- finds nothing. The gate record is never
        # pruned, so it is the right place to freeze this.
        for k, v in (job_facts(a.job_id) or {}).items():
            payload.setdefault(k, v)
        # The queue prunes finished rows; if the job is already gone, the cwd
        # this hook was CALLED with is the same value, and signoff.py locates
        # the preflight ledger by it.
        payload.setdefault("job_cwd", str(cwd))
        payload.setdefault("job_verify", a.verify or None)
        # Freeze the one-line task intent into the record so the completed-code
        # drop can render it later from merge_review too, which only has the
        # REVIEW job's task file (a.task_file there is not the parent's).
        if a.task_file and not payload.get("task_intent"):
            _intent = _one_line_intent(a.task_file)
            if _intent:
                payload["task_intent"] = _intent

        # Evaluate sign-off requirements and add them to the gate record.
        # diff_path is None when the gate skipped before a diff existed (no task
        # file, not a git tree); say so instead of raising NameError into the
        # advisory catch below and recording a misleading error string.
        try:
            if diff_path is None:
                payload["signoff_error"] = ("not evaluated: the gate skipped before a "
                                            "diff existed (" + str(payload.get("reason", ""))[:120] + ")")
            else:
                signoff_cmd = ["python3", str(SIGNOFF),
                               "--evaluate", a.job_id, "--diff", str(diff_path)]
                if a.task_file:
                    signoff_cmd.extend(["--task-file", a.task_file])
                r = subprocess.run(signoff_cmd, capture_output=True, text=True, timeout=60)
                if r.returncode == 0:
                    try:
                        so = _signoff_record(a.job_id)
                        if so:
                            payload["signoff_required"] = so.get("required", False)
                            payload["signoff_reviewer"] = so.get("reviewer", "")
                            payload["signoff_verdict"] = so.get("verdict", None)
                        else:
                            payload["signoff_error"] = ("evaluate exited 0 but wrote no "
                                                        "record for this job")
                    except Exception as e:
                        payload["signoff_error"] = f"load: {type(e).__name__}: {e}"[:200]
                else:
                    payload["signoff_error"] = (
                        f"evaluate exited {r.returncode}: "
                        f"{((r.stderr or r.stdout or '').strip()[-160:])}")
        except Exception as e:
            # Sign-off evaluation must never FAIL the gate -- it is advisory. But
            # swallowing the reason made it undiagnosable: job 08c288f0d675 landed
            # with no signoff_* keys at all and nothing anywhere said why, so
            # "sign-off was not required" and "the sign-off step broke" looked
            # identical from the record. Record it, still don't raise.
            payload["signoff_error"] = f"{type(e).__name__}: {e}"[:200]
        # Always state the outcome, even when no sign-off is needed. An ABSENT
        # key cannot distinguish "not required" from "never ran".
        payload.setdefault("signoff_required", None)
        payload.setdefault("signoff_verdict", None)

        # Autonomous sign-off. Written FIRST so signoff.py reads the finished
        # record: every condition it checks (verdict, counts, verify_exit,
        # verify_failed_at_baseline, untrusted, verify_relevance ...) lives in
        # this payload, and passing a half-built one would decide on absent
        # evidence -- which, since every missing key reads as "condition not
        # met", would fail closed but for the wrong reason and look like a
        # policy result.
        gate_json.write_text(json.dumps(payload, indent=1))
        if payload.get("signoff_required") and not payload.get("signoff_verdict"):
            try:
                r2 = subprocess.run(
                    ["python3", str(SIGNOFF),
                     "--auto", a.job_id, "--gate", str(gate_json),
                     "--diff", str(diff_path)],
                    capture_output=True, text=True, timeout=60)
                if r2.stdout:
                    print(r2.stdout, end="")
                if r2.returncode != 0:
                    payload["signoff_error"] = (
                        f"auto exited {r2.returncode}: "
                        f"{((r2.stderr or r2.stdout or '').strip()[-160:])}")
                so = _signoff_record(a.job_id)
                payload["signoff_verdict"] = so.get("verdict")
                payload["signoff_reviewer"] = so.get("reviewer", "")
                if so.get("auto_blocked_by"):
                    payload["signoff_auto_blocked_by"] = so["auto_blocked_by"]
                # SHADOW mode leaves verdict None by design; surface the
                # harness's would-be decision so the record shows sign-off RAN.
                if so.get("shadow_decision"):
                    payload["signoff_shadow_decision"] = so["shadow_decision"]
                    payload["signoff_shadow_reasons"] = (so.get("shadow_reasons") or [])[:6]
            except Exception as e:
                payload["signoff_error"] = f"auto: {type(e).__name__}: {e}"[:200]

        gate_json.write_text(json.dumps(payload, indent=1))
        v = payload.get("verdict", "?")
        n = (payload.get("counts") or {}).get("code_high", 0)
        # The one-line print is what a human actually reads. A check that
        # ABSTAINED has to appear here or the abstain is invisible in practice,
        # no matter how carefully the JSON records it.
        nc = payload.get("not_checked") or []
        nc_s = (" not_checked=" + ",".join(x.split(" (")[0] for x in nc)) if nc else ""
        ut = " UNTRUSTED=" + ";".join(payload["untrusted"]) if payload.get("untrusted") else ""
        print(f"[gate] {a.job_id} verdict={v} code_high={n}{nc_s}{ut} -> {gate_json}")
        # SCOPE-CREEP SHADOW (additive, log-only, never changes payload/verdict/exit): flags a diff that adds a new
        # thread/scheduler/env var/route the task never named, or any new runtime surface in a big unsigned change.
        # SCOPE_CREEP_DISABLE=1 skips it entirely.  See scope_creep.py (incident 4a454d6).
        try:
            if os.environ.get("SCOPE_CREEP_DISABLE", "0") in ("", "0", "false", "no") and not TEST_MODE:
                import scope_creep as _sc
                _spec = str(payload.get("task_intent") or "")
                try:
                    if getattr(a, "task_file", None):
                        _spec += "\n" + Path(a.task_file).read_text()
                except Exception:
                    pass
                _w, _r = _sc.check(Path(diff_path).read_text(), _spec, payload,
                                   {"path": "gate", "job": a.job_id, "label": payload.get("job_label")})
                if _r and _r.get("flag"):
                    print(f"[gate] {_sc.EVENT_SHADOW} {a.job_id} {','.join(_r['reasons'])}: {' | '.join(_r['evidence'][:2])}")
        except Exception:
            pass
        # POSITIVE CONTACT on a non-pass verdict. Advisory and
        # best-effort: wrapped whole, never affects the exit code.
        try:
            # 'pass-pending-review' is a CLEAN pass whose model review has not
            # landed yet -- it is not-yet-non-pass, not a finding (finding #4).
            # Notifying here fired for every clean job awaiting review and
            # polluted NON-PASS-GATES.md; the real non-pass surfaces later, from
            # merge_review, if the review flips it to fail (code_high>=1).
            if str(v) not in ("pass", "skipped", "pass-pending-review") and not TEST_MODE:
                _notify_non_pass(a.job_id, payload, gate_json)
        except Exception:
            pass
        # the operator's at-a-glance surface. Best-effort and last: it is a VIEW, so a
        # failure here loses nothing (rebuild with --all), and it must never be
        # able to affect the gate's own exit code -- rule 2 again.
        try:
            if not TEST_MODE:
                subprocess.run(["python3", str(BIN / "handoff-emit.py"),
                                "--job-id", a.job_id], capture_output=True, timeout=120)
        except Exception:
            pass
        # AUTO-FIX at emit ONLY when this verdict is already terminal because NO
        # review will land (review enqueue-failed, or a skip that produced no
        # review). The normal path enqueues a review (review=="pending") and the
        # decision fires from merge_review instead -- firing here too would
        # double-classify off the pre-review snapshot. A verify-red job whose
        # review enqueue FAILED would otherwise never get an auto-fix decision.
        try:
            if payload.get("review") != "pending":
                autofix_consider(a.job_id, payload, gate_json, out_dir)
                escalation_triage_consider(a.job_id, payload, gate_json)
                auto_pipeline_consider(a.job_id, payload, gate_json)
                auto_land_consider(a.job_id, payload, gate_json)
                slice_pipeline_consider(a.job_id, payload, gate_json)
                runstatus_janitor_consider(a.job_id, payload, gate_json)
                land_ingest_consider(a.job_id, payload)
        except Exception:
            pass
        # COMPLETED-CODE-DROP at the SAME terminality signal auto-fix uses: a
        # verdict is terminal here only when no review will land (review !=
        # "pending"). A pending review re-fires this hook via merge_review, which
        # drops then -- dropping here too would land a non-terminal snapshot.
        try:
            if payload.get("review") != "pending":
                drop_completed_code(a.job_id, payload, out_dir)
        except Exception:
            pass
        return 0        # ALWAYS 0 -- see rule 2

    if not a.task_file or not Path(a.task_file).expanduser().exists():
        return emit({"verdict": "skipped",
                     "reason": "no task file, so completeness and scope cannot be judged"})

    scaffold_excluded: list = []
    _chain_base = autofix_chain_base(a.job_id, cwd)
    # TREE RACE (b90e77ace2b2): the driver's authoring self-check APPLIES refimpl.py to the
    # tracked target and reverts it a few seconds later, under the worktree's tree lock.
    # `git diff HEAD` taken in that window put +15 lines of the reference solution into the
    # job's diff (a job that never touched the target). Read the diff under the same lock.
    # A lock held past the window is not fatal here: proceed, as before.
    diff_path, how = get_diff_locked(cwd, out_dir / f"{a.job_id}.diff",
                                     scaffold_digests(cwd), scaffold_excluded, base=_chain_base)
    if _chain_base and diff_path is not None:
        how = f"{how} [auto-fix chain: cumulative vs root baseline {_chain_base[:12]}]"
    if diff_path is None:
        # An EMPTY diff on a coding job whose own verify exited 0 is not "nothing
        # to review": the verify was green without the job changing anything (#2).
        # Record that as the decidable fail it is; a non-git/absent tree, or a job
        # with no green verify to contradict, stays a plain skip.
        _vx0 = a.verify_exit if a.verify_exit is not None else job_verify_exit(a.job_id)
        if how.startswith("no changes to review") and _vx0 == 0:
            _npc0 = no_product_change_finding(set(), a.job_label, _vx0)
            if _npc0:
                _pl0 = {"verdict": "pass", "issues": [], "reason": how, "diff_source": how,
                        "verify_exit_reported": _vx0}
                apply_finding(_pl0, _npc0)
                print(f"[gate] {a.job_id} NO PRODUCT CHANGE: empty diff, verify exit 0 "
                      f"-> fail (not skipped)")
                return emit(_pl0)
        return emit({"verdict": "skipped", "reason": how})

    # --- the DECIDABLE checks: pure Python, no GPU, run inline ---------------
    cmd = ["python3", str(GATE), "--task-file", str(Path(a.task_file).expanduser()),
           "--diff", str(diff_path), "--cwd", str(cwd), "--json", "--no-review"]
    # HARNESS-AUTHORING jobs (ollama-dispatch-auto auto-author-*/auto-refine-*)
    # SEAL the harness then edit it, so its scaffold files show as tracked scope
    # concerns and land the job at CONCERNS for no real defect. Tell the gate to
    # suppress those -- but ONLY for these jobs. A normal CODING job carries no
    # such prefix, so a coding diff that edits verify.sh/check_literals.py/fixture
    # (e.g. an auto-repair round weakening the check to force a green) stays a
    # blocking concern. That asymmetry is the repair guardrail; see gate.py (5).
    if a.job_label.startswith(("auto-author-", "auto-refine-")):
        cmd += ["--harness-authoring"]
    if a.verify:
        cmd += ["--verify", a.verify]
    # DID THE JOB'S OWN VERIFY PASS? gate.py has taken --verify-exit since it was
    # written and nothing ever passed it, so the verdict never saw the single
    # most decisive fact about the job. Read it off the record, same as
    # launch_baseline, so the queue's integration stays one call.
    vexit = a.verify_exit if a.verify_exit is not None else job_verify_exit(a.job_id)
    if vexit is not None:
        cmd += ["--verify-exit", str(vexit)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        payload = json.loads(r.stdout)
        payload["diff_source"] = how
        if scaffold_excluded:
            payload["scaffold_excluded"] = scaffold_excluded
    except Exception as e:
        return emit({"verdict": "error", "reason": f"{type(e).__name__}: {str(e)[:200]}"})

    # NO PRODUCT CHANGE (#2): a coding diff that touches only scaffold is no change.
    _npc = no_product_change_finding(_diff_changed_files(diff_path), a.job_label, vexit)
    if _npc:
        apply_finding(payload, _npc)
        print(f"[gate] {a.job_id} NO PRODUCT CHANGE -> {payload['verdict']}: "
              f"{_npc['what'][:160]}")

    # WAS THE STARTING TREE CLEAN? Read from the queue's stamp so the queue's
    # integration stays ONE CALL -- ollama-queue.py adds the stamp and changes
    # nothing about how it invokes this hook.
    if vexit is None:
        payload.setdefault("not_checked", []).append(
            "verify-exit (the queue record carries no terminal exit_code for this "
            "job, so whether its own verify passed is unknown)")
    _lb_eff = effective_launch_baseline(launch_baseline(a.job_id), cwd, a.job_label,
                                        a.launch_dirty)
    apply_baseline(payload, _lb_eff, a.launch_dirty, baseline_at(a.job_id))

    # --- verify RELEVANCE on the model's ACTUAL diff ---------------------------
    # The preflight measured relevance against the REFERENCE impl and wrote it to
    # the ledger. This measures it against the code being signed off: mutate the
    # lines the model added, in place, and ask whether the job's own --verify
    # kills them. A green verify that lets the model's added lines be flipped,
    # negated or deleted without going red certified nothing. Runs only when
    # there is a verify to run; abstains VISIBLY otherwise (not_checked), because
    # an absent block reads as "never measured" to signoff.py and holds.
    # See relevance_verify_cmd() -- an authoring/refine job's own verify is the
    # harness self-check, not a discriminator; that gets substituted there.
    if _chain_base:
        payload["diff_base"] = _chain_base
    # VERDICT IDENTITY (2026-10-06): name the job / diff / base / product-file
    # hashes this verdict is ABOUT, read before relevance mutates anything, so
    # apply_fix and the slicer can refuse to land on a verdict whose code changed.
    stamp_judged_identity(payload, a.job_id, cwd, diff_path,
                          base=_chain_base or (payload.get("launch_baseline") or {}).get("head") or "")
    # measure_relevance resolves the CPU-lane bundle key from payload["job_id"] (the job's REAL queue
    # bundle tag); emit() used to be the only place that set it, i.e. AFTER this ran -> a label key.
    payload.setdefault("job_id", a.job_id)
    measure_relevance(payload, cwd, relevance_verify_cmd(a.job_label, a.verify),
                      a.task_file, base=_chain_base)

    # --- the MODEL review: ENQUEUED, never called directly --------------------
    review_dir = out_dir / f"{a.job_id}-review"
    review_dir.mkdir(parents=True, exist_ok=True)
    # The task file existed at the top of main(); it may be GONE by now (worktree
    # reaped mid-gate) -- degrade, never die without a verdict. See _read_review_intent.
    _intent_text, _intent_note = _read_review_intent(a.task_file)
    if _intent_note:
        payload.setdefault("not_checked", []).append(_intent_note)
        print(f"[gate] {a.job_id} task file unreadable at review-enqueue time -- "
              f"reviewing the diff without the TASK: {_intent_note[:200]}")
    (review_dir / "task.json").write_text(json.dumps({
        "mode": "review", "diff": str(diff_path),
        "intent": review_intent(_intent_text),
        "context": ("Automated pre-gate on a completed dispatch. Report only "
                    "defects this change introduces."
                    + REVIEW_FIXTURE_RULE
                    + _slice_review_context(a.job_label)),
        "repo": str(cwd),
        "ref_budget": ref_budget_for(PREGATE_NUM_CTX),
        "base": payload.get("diff_base") or (payload.get("launch_baseline") or {}).get("head") or ""}, indent=1))
    label = f"gate-{a.job_id}"          # the 'gate-' prefix is the LOOP GUARD
    # TWO-TIER: the cheap PRE-gate runs on Unraid (qwen3:14b @ 6144), not the
    # inbound job's --model/--host. A non-pass result escalates to the Studio
    # 27B re-gate (see _escalate_regate); a pass stands, non-terminal.
    payload["review_tier"] = "pregate"
    payload["review_model"] = PREGATE_MODEL
    payload["review_host"] = PREGATE_HOST
    enq = ["python3", str(BIN / "ollama-queue.py"), "enqueue",
           "--model", PREGATE_MODEL, "--host", PREGATE_HOST,
           "--num-ctx", str(PREGATE_NUM_CTX),
           "--cwd", str(review_dir), "--task-file", str(review_dir / "task.json"),
           "--runner", str(BIN / "code-review-agent.py"), "--label", label, "--front",
           "--allow-no-verify"]  # advisory scope/diff review: no --verify by design
    if TEST_MODE:
        # A canary must never enqueue onto the live queue. Visible, not silent.
        payload["review"] = "skipped (GATE_TEST_MODE)"
        payload["review_label"] = label
        payload["review_dir"] = str(review_dir)
        return emit(payload)
    try:
        e = subprocess.run(enq, capture_output=True, text=True, timeout=120)
        ok = e.returncode == 0
        payload["review"] = "pending" if ok else "enqueue-failed"
        payload["review_label"] = label
        payload["review_dir"] = str(review_dir)
        if not ok:
            payload["review_error"] = (e.stderr or e.stdout or "")[-200:]
    except Exception as ex:
        payload["review"] = "enqueue-failed"
        payload["review_error"] = f"{type(ex).__name__}: {str(ex)[:160]}"

    # A pending review must NOT read as a clean pass.
    if payload.get("review") == "pending" and payload.get("verdict") == "pass":
        payload["verdict"] = "pass-pending-review"
    return emit(payload)


def _self_test() -> bool:
    """Unit-test the terminal-facts readers -- specifically the SIDECAR-FIRST
    fallback that certifies a job whose LIVE row is still non-terminal (the
    reap-vs-lock.save race, finding #2), while preserving the correct abstain
    when nothing terminal is available. No GPU, no network; writes only into a
    throwaway temp dir. Same PASS/FAIL convention as ollama-queue.py --self-test."""
    import tempfile as _tf, shutil as _sh
    global QUEUE_STATE, BIN, STATE_DIR, COMPLETED_ROOT, AUTO_PIPELINE_MODE
    global RUNSTATUS_JANITOR_MODE, _SLICE_RUNS_DIR, ESCALATION_TRIAGE_MODE
    ok = True

    def check(name, got, want):
        nonlocal ok
        if got == want:
            print(f"PASS {name}")
        else:
            ok = False
            print(f"FAIL {name}: got {got!r} want {want!r}")

    _orig_qs, _orig_bin, _orig_sd, _orig_cr = QUEUE_STATE, BIN, STATE_DIR, COMPLETED_ROOT
    _oldenv = os.environ.get("OLLAMA_DISPATCH_MODEL_LADDER")
    _td = Path(_tf.mkdtemp(prefix="gate-selftest-"))
    try:
        BIN = _td
        STATE_DIR = _td
        QUEUE_STATE = _td / "ollama-queue-state.json"
        COMPLETED_ROOT = _td / "Completed"
        (_td / "ollama-queue-logs").mkdir(parents=True, exist_ok=True)

        def _state(jobs):
            QUEUE_STATE.write_text(json.dumps({"jobs": jobs}))

        def _sidecar(jid, rec):
            (_td / "ollama-queue-logs" / f"{jid}.done.json").write_text(json.dumps(rec))

        # --- NO-DIFF GUARD: research/--runner jobs are never code-gated --------
        # Regression guard for the bonsai bake-off incident (operator 2026-09-18).
        check("ungateable: task_kind=research",
              job_is_ungateable({"id": "u1", "task_kind": "research"}), True)
        check("ungateable: --runner job",
              job_is_ungateable({"id": "u2", "task_kind": "coding",
                                 "runner": "/Users/example/bin/bakeoff-runner.py"}), True)
        check("ungateable: plain coding dispatch is STILL gated",
              job_is_ungateable({"id": "u3", "task_kind": "coding", "runner": None}), False)
        check("ungateable: unset task_kind is STILL gated",
              job_is_ungateable({"id": "u4"}), False)
        check("ungateable: empty-string runner is STILL gated",
              job_is_ungateable({"id": "u5", "task_kind": "coding", "runner": ""}), False)
        check("ungateable: a missing job record is NOT ungateable",
              job_is_ungateable(None), False)

        # (a) THE RACE: the live row still says running / exit_code null (the
        # daemon has not lock.save()'d the flip yet), but the terminal sidecar
        # says done exit 0. Sidecar-first must CERTIFY, not abstain.
        _state([{"id": "race1", "status": "running", "exit_code": None,
                 "launch_baseline": {"head": "abc", "dirty": 0}}])
        _sidecar("race1", {"id": "race1", "status": "done", "exit_code": 0,
                           "label": "dash-fix", "launch_baseline": {"head": "abc", "dirty": 0},
                           "launched_by": "uds:/tmp/cc-socks/123.sock",
                           "launched_by_session": "sess-xyz"})
        check("sidecar-first certifies verify-exit over non-terminal live row",
              job_verify_exit("race1"), 0)
        check("sidecar-first certifies launch-baseline over non-terminal live row",
              (launch_baseline("race1") or {}).get("dirty"), 0)
        check("job_facts reads terminal sidecar, not the running live row",
              job_facts("race1").get("job_status"), "done")
        check("_job_field falls back to sidecar for launched_by (finding #5)",
              _job_field("race1", "launched_by"), "uds:/tmp/cc-socks/123.sock")

        # (b) ABSTAIN PRESERVED: non-terminal live row, NO sidecar -> None, never
        # a false PASS/exit.
        _state([{"id": "run2", "status": "running", "exit_code": None}])
        check("no sidecar + running live row -> abstain (None)",
              job_verify_exit("run2"), None)

        # (c) ABSTAIN PRESERVED: row pruned (absent) + no sidecar -> None.
        _state([])
        check("pruned row + no sidecar -> abstain (None)",
              job_verify_exit("gone3"), None)

        # (d) terminal live row, no sidecar -> still certifies (normal fast path).
        _state([{"id": "done4", "status": "done", "exit_code": 0}])
        check("terminal live row certifies with no sidecar",
              job_verify_exit("done4"), 0)

        # (e) a FAILED sidecar certifies its nonzero exit (not swallowed to None).
        _state([])
        _sidecar("fail5", {"id": "fail5", "status": "failed", "exit_code": 2})
        check("failed sidecar certifies nonzero exit", job_verify_exit("fail5"), 2)

        # (f) launch_baseline from a still-RUNNING live row with no sidecar: it is
        # stamped at launch, so it stays readable (not gated on terminal status).
        _state([{"id": "run6", "status": "running", "exit_code": None,
                 "launch_baseline": {"head": "def", "dirty": 3}}])
        check("launch_baseline readable from running live row (no sidecar)",
              (launch_baseline("run6") or {}).get("dirty"), 3)

        # (g) baseline_at: WHEN the reading was taken, read the same way.
        _state([{"id": "at1", "status": "running", "baseline_at": "launch",
                 "launch_baseline": {"head": "a", "dirty": 0}}])
        check("baseline_at reads 'launch' off the live row", baseline_at("at1"), "launch")
        _state([{"id": "at2", "status": "running", "baseline_at": "enqueue",
                 "launch_baseline": {"head": "a", "dirty": 0}}])
        check("baseline_at reads 'enqueue'", baseline_at("at2"), "enqueue")
        _state([{"id": "at3", "status": "running",
                 "launch_baseline": {"head": "a", "dirty": 0}}])
        check("a job predating the field abstains (None, never a default)",
              baseline_at("at3"), None)
        _state([{"id": "at4", "status": "running", "baseline_at": "whenever",
                 "launch_baseline": {"head": "a", "dirty": 0}}])
        check("an unrecognised value is NOT trusted as launch-true",
              baseline_at("at4"), None)

        # --- apply_baseline x baseline_at: a clean reading only counts if it
        # was taken at LAUNCH. This is the rt-costco hole, at the consumer end.
        def _ab(dirty, at, override=-1):
            p = {"verdict": "pass"}
            apply_baseline(p, {"head": "h", "dirty": dirty}, override, at)
            return p
        _p = _ab(0, "launch")
        check("clean AND measured at launch: genuinely clean, nothing recorded",
              (_p.get("not_checked"), _p.get("issues"), _p["verdict"]),
              (None, None, "pass"))
        _p = _ab(0, "enqueue")
        check("clean but measured at ENQUEUE: the abstain is RECORDED, not passed "
              "off as clean (a tree dirtied between enqueue and launch is invisible "
              "to this reading -- the rt-costco cascade exactly)",
              len(_p.get("not_checked") or []), 1)
        check("...and it stays an abstain, NOT an invented finding: no issue, no "
              "demotion (unknown must not condemn code that may be fine)",
              (_p.get("issues"), _p["verdict"]), (None, "pass"))
        _p = _ab(0, None)
        check("a pre-field job is treated the same as enqueue: unknown, recorded",
              len(_p.get("not_checked") or []), 1)
        # A DIRTY reading is real evidence WHENEVER it was taken -- enqueue-dirty
        # means the tree was already dirty even earlier. Provenance must not soften it.
        for _at in ("launch", "enqueue", None):
            _p = _ab(2, _at)
            check(f"dirty=2 measured at {_at!r} still flags high/input + untrusted",
                  (len(_p.get("issues") or []), _p["verdict"], bool(_p.get("untrusted"))),
                  (1, "concerns", True))
        # The operator's own count stands whatever the queue recorded.
        _p = _ab(0, "enqueue", override=0)
        check("an explicit --launch-dirty 0 override is exempt from the provenance "
              "abstain (the operator measured it themselves)",
              _p.get("not_checked"), None)
        check("provenance is recorded on the payload for the reader",
              _ab(0, "launch").get("baseline_at"), "launch")

        # A correct reader the CALL SITE stops using leaves the hole exactly where
        # it was, and every check above still passes because they call
        # apply_baseline directly. So assert the wiring, in the source. Scoped to
        # the production half with the needle built at runtime: a source-grep test
        # that can find its own search string tests nothing (learned the hard way
        # on ollama-queue.py's launch re-stamp earlier tonight).
        _gsrc = Path(__file__).read_text().split("def _self_test", 1)[0]
        check("scoping control: the self-test half is excluded",
              "def _self_test" not in _gsrc, True)
        _needle = "a.launch_dirty, " + "baseline_at(a.job_id))"
        check("the gate actually PASSES the provenance into apply_baseline",
              _needle in _gsrc, True)

        # --- AUTO-FIX classifier (pure; no state needed) --------------------
        def _cls(p):
            return autofix_classify(p)["action"], autofix_classify(p)["class"]
        # (b) sealed verify RED -> requeue
        check("verify-red -> requeue",
              _cls({"verdict": "fail", "issues": [
                  {"category": "code", "severity": "high", "source": "verify-exit",
                   "what": "verify exit 1"}]}), ("requeue", "verify-red"))
        # (a) scope-only concerns -> requeue
        check("scope-only concerns -> requeue",
              _cls({"verdict": "concerns", "issues": [
                  {"category": "input", "severity": "low", "source": "scope",
                   "what": "edited file not named"}]}), ("requeue", "scope"))
        # (c) reviewer-only code-high, no red verify -> escalate (false-FAIL class)
        check("reviewer code-high, no verify -> escalate",
              _cls({"verdict": "fail", "issues": [
                  {"category": "code", "severity": "high", "source": "review",
                   "what": "reviewer says bug"}]}), ("escalate", "reviewer-code-high"))
        # #2 no-product-change: decidable, but a re-run cannot fix it -> escalate
        check("no-product-change -> escalate (never requeue)",
              _cls({"verdict": "fail", "issues": [
                  {"category": "code", "severity": "high", "source": "no-product-change",
                   "what": "scaffold-only diff"}]}), ("escalate", "no-product-change"))
        check("product_files strips every scaffold name and keeps product paths",
              product_files({"TASK.md", "verify.sh", "verify_impl.mts", "verify.test.ts",
                             "test_fixture.py", "refimpl.py", ".preflight-state.json",
                             "src/x.py", "arr-webhook.py"}),
              {"src/x.py", "arr-webhook.py"})
        check("a scaffold-only coding diff is a code-high no-product-change finding",
              (no_product_change_finding({"TASK.md"}, "aw-sched-runner-s23", 0) or {}).get("source"),
              "no-product-change")
        check("...whose text names the green-at-baseline consequence when verify exit was 0",
              "already green at the launch baseline" in
              no_product_change_finding({"TASK.md"}, "plan-s1", 0)["what"], True)
        check("...and does not claim it when the verify was red",
              "already green" in no_product_change_finding(set(), "plan-s1", 1)["what"], False)
        check("a diff with a product file is NOT flagged",
              no_product_change_finding({"TASK.md", "src/x.py"}, "plan-s1", 0), None)
        check("an authoring job's scaffold-only diff is its deliverable, not a defect",
              no_product_change_finding({"TASK.md", "test_fixture.py"},
                                        "auto-author-plan-s1", 0), None)
        _pl = {"verdict": "pass", "issues": [], "counts": {"code_high": 0, "code": 0,
                                                            "input": 0, "total": 0}}
        apply_finding(_pl, no_product_change_finding({"TASK.md"}, "p-s1", 0))
        check("apply_finding flips a clean pass to FAIL with recomputed counts",
              (_pl["verdict"], _pl["counts"]["code_high"], _pl["counts"]["total"]),
              ("fail", 1, 1))
        # #8 review intent: the reviewer sees Required change / Must contain / Scope
        _task = ("# Task\n\nintro line\n\n## Confirmed defect (observed, not suspected)\n"
                 + "d" * 1500 + "\n## Entry point\n`f()`\n## Required change\n"
                 + "r" * 800 + "\n## Must contain\n- `upsert_schedule(`\n## Scope\n"
                 + "s" * 300 + "\n## Loop instruction\n" + "l" * 3000 + "\n")
        _ri = review_intent(_task, cap=4000)
        check("review_intent keeps Must contain and Scope inside the cap",
              ("## Must contain" in _ri and "upsert_schedule(" in _ri
               and "## Scope" in _ri and "## Required change" in _ri), True)
        check("...within the cap", len(_ri) <= 4000 + 80, True)
        check("...dropping the low-priority section first and saying so",
              "## Loop instruction" not in _ri and "omitted to fit" in _ri, True)
        check("...and preserves document order among kept sections",
              _ri.index("## Required change") < _ri.index("## Must contain") < _ri.index("## Scope"),
              True)
        check("a short TASK.md is returned untouched", review_intent("## Scope\nx\n"), "## Scope\nx")
        check("the old [:1500] cut lost Must contain on this TASK.md (the bug being fixed)",
              "## Must contain" in _task[:1500], False)
        # (c) invariant-guard removed+re-added-literal FP -> escalate, never requeue
        check("invariant-guard FP -> escalate",
              _cls({"verdict": "concerns", "issues": [
                  {"category": "input", "severity": "high", "source": "invariant-guard",
                   "what": "removed X under invariant comment"}]}),
              ("escalate", "undecidable"))
        # dirty/unattributable baseline -> escalate, NEVER requeue (loop seed)
        check("untrusted baseline -> escalate",
              _cls({"verdict": "concerns", "untrusted": ["dirty (2)"], "issues": [
                  {"category": "input", "severity": "high", "source": "launch-baseline",
                   "what": "dirty tree"}]}), ("escalate", "unattributable"))
        # scope RIDING WITH an undecidable input finding -> NOT scope-requeue
        check("scope + invariant-guard -> escalate (not scope-requeue)",
              _cls({"verdict": "concerns", "issues": [
                  {"category": "input", "severity": "low", "source": "scope", "what": "x"},
                  {"category": "input", "severity": "high", "source": "invariant-guard",
                   "what": "y"}]}), ("escalate", "undecidable"))
        # clean verdicts -> none
        check("pass -> none", _cls({"verdict": "pass", "issues": []}),
              ("none", "clean"))
        check("pass-pending-review -> none",
              _cls({"verdict": "pass-pending-review", "issues": []}), ("none", "clean"))

        # --- RELEVANCE / SUSPECT-THE-GRADER (Task D) -------------------------
        # SURVIVING mutants: the fixture failed to kill a plausible-wrong build ->
        # grader-suspect-relevance (escalate), the owed repair is a harness refine.
        check("relevance survivors -> grader-suspect-relevance (escalate)",
              _cls({"verdict": "concerns", "issues": [], "verify_relevance": {
                  "verdict": "unproven",
                  "survivors": [{"file": "app.py", "line": 12, "mutation": "return None"}]}}),
              ("escalate", "grader-suspect-relevance"))
        check("relevance survivors escalate carries the concrete survivor detail",
              "app.py:12" in " ".join(autofix_classify({"verdict": "concerns",
                  "issues": [], "verify_relevance": {"verdict": "unproven",
                  "survivors": [{"file": "app.py", "line": 12,
                                 "mutation": "return None"}]}})["reasons"]), True)
        # relevance UNPROVEN with NO survivor to act on -> grader-suspect-unproven:
        # the harness could not MEASURE relevance (verify-relevance/spec defect).
        check("relevance unproven, 0 survivors -> grader-suspect-unproven (escalate)",
              _cls({"verdict": "concerns", "issues": [], "verify_relevance": {
                  "verdict": "unproven", "survivors": []}}),
              ("escalate", "grader-suspect-unproven"))
        # both grader-suspect classes map to the queue's relevance categories:
        check("grader-suspect-relevance -> undecidable-relevance category",
              autofix_escalation_category("grader-suspect-relevance"),
              "undecidable-relevance")
        check("grader-suspect-unproven -> undecidable-unproven category",
              autofix_escalation_category("grader-suspect-unproven"),
              "undecidable-unproven")
        # CONSERVATISM revert-test: a REAL code finding riding a relevance-unproven
        # verdict must NOT be laundered into a grader-suspect escalate -- code takes
        # priority (reviewer-code-high false-FAIL class), grader-suspicion does not
        # excuse it.
        check("code finding + relevance unproven -> code path, NOT grader-suspect",
              _cls({"verdict": "fail", "verify_relevance": {"verdict": "unproven",
                  "survivors": [{"file": "a.py", "line": 1}]}, "issues": [
                  {"category": "code", "severity": "high", "source": "review",
                   "what": "reviewer says bug"}]}),
              ("escalate", "reviewer-code-high"))
        # a coding concerns job with NO measured relevance block is unchanged
        # (falls through to undecidable, not grader-suspect):
        check("no relevance block -> undecidable (grader-suspect does not fire)",
              _cls({"verdict": "concerns", "issues": [
                  {"category": "input", "severity": "high", "source": "completeness",
                   "what": "missing case"}]}), ("escalate", "undecidable"))

        # --- GUARDRAIL: a 'fix' achieved by editing the HARNESS is REJECTED -----
        # The absolute guardrail: an auto-repair may edit TARGET code only. Prove a
        # verify.sh edit cannot be laundered into a landable success. When the gate
        # sees a coding job that touched verify.sh it raises a scope/code finding on
        # it (harness_authoring=False for coding), so the resulting payload is (i)
        # NOT clean -> classify_advance refuses to apply it, and (ii) NOT accepted by
        # classify_autofix as a pass -- it escalates/holds, never 'none/clean'. The
        # gate.py suppression-behind-authoring self-test proves the finding is RAISED;
        # here we prove the auto-pipeline REFUSES to treat that payload as a fix.
        _tamper = {"verdict": "concerns", "issues": [
            {"category": "input", "severity": "high", "source": "scope",
             "file": "verify.sh", "what": "edited the sealed verify.sh (harness)"},
            {"category": "code", "severity": "high", "source": "review",
             "file": "verify.sh", "what": "weakened the gate to force a pass"}]}
        _t_act, _t_cls = _cls(_tamper)
        check("guardrail: verify.sh-edit 'fix' is NOT accepted as clean (escalates)",
              _t_act, "escalate")
        check("guardrail: verify.sh-edit payload is NOT gate-clean",
              _gate_is_clean(_tamper), False)
        check("guardrail: classify_advance REFUSES to apply a verify.sh-edit payload",
              classify_advance(_tamper, "somefix", "resell-tracker")["action"], "none")
        check("guardrail: even a 'pass' verdict with a verify.sh code finding "
              "is not clean (cannot be laundered)",
              _gate_is_clean({"verdict": "pass", "issues": [
                  {"category": "code", "severity": "high", "source": "scope",
                   "file": "verify.sh", "what": "edited verify.sh"}]}), False)

        # --- LOOP GUARD round parser: label marker recovers a torn field read ---
        _state([])  # no live row, no sidecar -> field read is None
        check("round from label marker when field unreadable",
              autofix_round("norow", {"job_label": "myfix [auto-fix r2]"}), 2)
        check("round 0 for an original dispatch (no marker, no field)",
              autofix_round("norow", {"job_label": "myfix"}), 0)
        # field and label disagree -> take the MAX (torn read can't lower it)
        _state([{"id": "r7", "auto_fix_round": 1}])
        check("round takes MAX(field,label) so a stale low value can't reset it",
              autofix_round("r7", {"job_label": "myfix [auto-fix r3]"}), 3)

        # --- ANTI-THRASH: non-reducing / repeat-finding round parks for human ---
        # fingerprint: total from counts.total; per-finding hashes stable+sorted.
        _fp_payload = {"counts": {"total": 2}, "issues": [
            {"source": "verify-exit", "file": "a.js", "what": "exit 1"},
            {"source": "scope", "file": "b.js", "what": "not named"}]}
        check("fingerprint total from counts.total",
              autofix_fingerprint(_fp_payload)[0], 2)
        check("fingerprint hashes are stable and count all issues",
              len(autofix_fingerprint(_fp_payload)[1]), 2)
        # round 0 has no parent -> never a thrash park.
        check("thrash-check: round 0 never parks (no parent)",
              autofix_thrash_check("rootA", 0, 5, ["x"]), None)
        # no ledger entry for the parent round -> fail-open (allow, cap bounds).
        check("thrash-check: missing parent record -> allow (None)",
              autofix_thrash_check("rootMissing", 1, 3, ["x"]), None)
        # seed r0 for rootA with total=3; a NON-REDUCING r1 (total 3) must park.
        _autofix_ledger_record("rootA", 0, "j0", 3, ["verify-exit|a.js|exit 1"])
        check("thrash-check: non-reducing round -> park (escalate reason)",
              autofix_thrash_check("rootA", 1, 3, ["scope|b.js|other"]) is not None, True)
        # a REDUCING r1 (total 1 < 3) with different findings -> allowed (None).
        check("thrash-check: reducing round -> allow (None)",
              autofix_thrash_check("rootA", 1, 1, ["scope|b.js|other"]), None)
        # SAME finding-set as parent, even at equal count -> park (fixed twice).
        _autofix_ledger_record("rootB", 0, "k0", 2, ["verify-exit|a.js|exit 1", "scope|b.js|x"])
        check("thrash-check: identical finding-set -> park",
              autofix_thrash_check("rootB", 1, 2,
                                    ["scope|b.js|x", "verify-exit|a.js|exit 1"]) is not None, True)
        # ledger record is idempotent: re-recording a round overwrites, not appends.
        _autofix_ledger_record("rootC", 0, "c0", 4, ["z"])
        _autofix_ledger_record("rootC", 0, "c0", 1, ["z"])
        check("ledger record idempotent per round (overwrite, not append)",
              (_autofix_ledger_read("rootC").get("rounds") or {}).get("0", {}).get("total"), 1)

        # --- EARLY SUSPECT-THE-GRADER: same finding-set from a DISTINCT model parks
        # immediately (no further ladder climb). r0 = one model, then a DIFFERENT
        # model reproduces the identical fingerprint.
        _autofix_ledger_record("rootGS", 0, "g0", 2,
                               ["verify-exit|a.js|exit 1"], "qwen3.8:27b-q4_K_M")
        check("grader-suspect: same findings under a DISTINCT model -> returns prior model",
              autofix_grader_suspect("rootGS", 1, ["verify-exit|a.js|exit 1"],
                                     "qwen3.6:35b-a3b-q4_K_M"), "qwen3.8:27b-q4_K_M")
        check("grader-suspect: same findings, SAME model -> None (plain thrash, may ladder once)",
              autofix_grader_suspect("rootGS", 1, ["verify-exit|a.js|exit 1"],
                                     "qwen3.8:27b-q4_K_M"), None)
        check("grader-suspect: different findings -> None",
              autofix_grader_suspect("rootGS", 1, ["scope|b.js|x"],
                                     "qwen3.6:35b-a3b-q4_K_M"), None)
        check("grader-suspect: empty fp -> None (fail-open)",
              autofix_grader_suspect("rootGS", 1, [], "qwen3.6:35b-a3b-q4_K_M"), None)
        # ledger now persists the model that produced each round's fingerprint.
        check("ledger record persists the round's model",
              (_autofix_ledger_read("rootGS").get("rounds") or {}).get("0", {}).get("model"),
              "qwen3.8:27b-q4_K_M")

        # --- MODEL-FALLBACK LADDER (Tier 1) pure functions -------------------
        # normalize: strings + dicts -> {model,host}; host defaults to studio;
        # empties/dupes dropped.
        _nl = _normalize_ladder(["A", {"model": "B", "host": "unraid"}, "",
                                 {"host": "x"}, "A"])
        check("ladder normalize: strings default host=studio",
              _nl[0], {"model": "A", "host": "studio"})
        check("ladder normalize: dict host respected", _nl[1],
              {"model": "B", "host": "unraid"})
        check("ladder normalize: empties + dupes dropped", len(_nl), 2)
        # plan: advance to the NEXT rung after current; skip tried; None when out.
        _L = _normalize_ladder(["A", "B", "C"])
        check("ladder plan: advance to the rung AFTER current",
              plan_model_ladder("A", [], _L), {"model": "B", "host": "studio"})
        check("ladder plan: skip an already-tried rung",
              plan_model_ladder("A", ["B"], _L), {"model": "C", "host": "studio"})
        check("ladder plan: current not in ladder -> start at the TOP",
              plan_model_ladder("Z", [], _L), {"model": "A", "host": "studio"})
        check("ladder plan: last rung -> exhausted (None)",
              plan_model_ladder("C", [], _L), None)
        check("ladder plan: all rungs tried -> exhausted (None)",
              plan_model_ladder("A", ["B", "C"], _L), None)
        # served-model dedupe: rungs that ALIAS to an already-run model are no-ops.
        _sv = lambda m, h: ("DB" if (":" in m and h == "studio") else m)
        _LD = _normalize_ladder(["q:1", "q:2", {"model": "u:3", "host": "unraid"}, "D"])
        check("ladder plan: aliased rung (same served model) skipped",
              plan_model_ladder("q:1", [], _LD, served=_sv), {"model": "u:3", "host": "unraid"})
        check("ladder plan: current served == a rung's bare id -> skipped",
              plan_model_ladder("DB", [], _normalize_ladder(["q:1", "D"]), served=_sv),
              {"model": "D", "host": "studio"})
        check("ladder plan: all rungs alias to tried -> exhausted (None)",
              plan_model_ladder("q:1", [], _normalize_ladder(["q:2", "DB"]), served=_sv), None)
        check("ladder plan: no served fn -> legacy name-only behaviour",
              plan_model_ladder("q:1", [], _LD), {"model": "q:2", "host": "studio"})
        # env override beats file/default; category mapping is stable.
        os.environ["OLLAMA_DISPATCH_MODEL_LADDER"] = "E0, E1 , E2"
        check("ladder load: env override wins, comma-split + trimmed",
              [e["model"] for e in load_model_ladder()], ["E0", "E1", "E2"])
        check("category: ladder-exhausted -> persistent-nogo",
              autofix_escalation_category("ladder-exhausted"), "persistent-nogo")
        check("category: gate-error -> rc2",
              autofix_escalation_category("gate-error"), "rc2")
        check("category: undecidable -> other",
              autofix_escalation_category("undecidable"), "other")

        # --- INTEGRATED: capability exhaustion engages the LADDER, not escalate --
        # A deterministic 3-rung ladder for the integrated cases below (env wins).
        os.environ["OLLAMA_DISPATCH_MODEL_LADDER"] = "L0,L1,L2"

        def _vrp():  # a verify-red payload (a decidable CAPABILITY failure)
            return {"verdict": "fail", "counts": {"total": 1}, "issues": [
                {"category": "code", "severity": "high", "source": "verify-exit",
                 "file": "a.js", "what": "exit 1 still"}]}

        # (T1a) THRASH now advances the ladder to the NEXT model instead of parking.
        # Job on rung L0, no rungs tried yet; classify=verify-red(requeue) but the
        # thrash guard fires -> model-fallback to L1 (same task+verify, full gate).
        _state([{"id": "jr1", "auto_fix_round": 1, "auto_fix_root": "rootD",
                 "label": "x [auto-fix r1]", "cwd": str(_td), "verify": "bash verify.sh",
                 "model": "L0"}])
        _autofix_ledger_record("rootD", 0, "d0", 1, ["verify-exit|a.js|exit 0"])
        _gj = _td / "ollama-queue-logs" / "jr1.gate.json"
        _pl = _vrp()
        autofix_consider("jr1", _pl, _gj, _td / "ollama-queue-logs")
        check("consider: thrash -> model-fallback requeue (Tier 1), not escalate",
              _pl.get("auto_fix_action"), "requeue")
        check("consider: fallback is labelled class=model-fallback",
              _pl.get("auto_fix_class"), "model-fallback")
        check("consider: fallback selects the NEXT ladder rung (L1)",
              _pl.get("auto_fix_fallback_model"), "L1")
        check("consider: fallback pins the rung host (studio)",
              _pl.get("auto_fix_fallback_host"), "studio")

        def _flag2(v, name):
            return v[v.index(name) + 1] if name in v else None
        check("fallback requeue argv carries --model = the next rung",
              _flag2(_pl.get("auto_fix_cmd_preview") or [], "--model"), "L1")
        check("fallback requeue argv reuses the SEALED --verify (no bypass)",
              _flag2(_pl.get("auto_fix_cmd_preview") or [], "--verify"), "bash verify.sh")
        check("fallback requeue argv threads --models-tried (includes L0)",
              "L0" in (_flag2(_pl.get("auto_fix_cmd_preview") or [], "--models-tried") or ""),
              True)

        # (T1b) LADDER EXHAUSTED -> park needs_opus (Tier 2). Job on the LAST rung
        # L2 with L0,L1 already tried; a capability failure past the cap has no rung
        # left -> escalate class=ladder-exhausted, category persistent-nogo, reason
        # lists the models tried, and NO requeue command is emitted.
        _state([{"id": "jrX", "auto_fix_round": 1, "auto_fix_root": "rootX",
                 "label": "x [auto-fix r1]", "cwd": str(_td), "verify": "bash verify.sh",
                 "model": "L2", "models_tried": ["L0", "L1"]}])
        _autofix_ledger_record("rootX", 0, "x0", 1, ["verify-exit|a.js|exit 0"])
        _pjx = _vrp()
        autofix_consider("jrX", _pjx, _td / "ollama-queue-logs" / "jrX.gate.json",
                         _td / "ollama-queue-logs")
        check("consider: ladder exhausted -> escalate",
              _pjx.get("auto_fix_action"), "escalate")
        check("consider: exhaustion class = ladder-exhausted",
              _pjx.get("auto_fix_class"), "ladder-exhausted")
        check("consider: exhaustion category = persistent-nogo",
              _pjx.get("auto_fix_escalate_category"), "persistent-nogo")
        check("consider: exhaustion reason lists the models tried",
              all(m in " ".join(_pjx.get("auto_fix_reasons") or [])
                  for m in ("L0", "L1", "L2")), True)
        check("consider: exhaustion emits an escalate (needs_opus) command",
              _pjx.get("auto_fix_escalate_cmd") is not None, True)
        check("consider: exhaustion emits NO requeue command",
              _pjx.get("auto_fix_cmd_preview"), None)
        check("consider: escalate command targets ollama-queue escalate",
              "escalate" in (_pjx.get("auto_fix_escalate_cmd") or []), True)
        # Task D: the escalation carries the FULL repair history (per-round ledger
        # summary + models tried) on the payload and inside the escalate --reason.
        check("consider: escalate records full repair history on the payload",
              "repair history" in (_pjx.get("auto_fix_history") or ""), True)
        check("consider: repair history names the recorded round + models tried",
              all(s in (_pjx.get("auto_fix_history") or "")
                  for s in ("r0=", "L0", "L1", "L2")), True)
        _esc_reason = " ".join(_pjx.get("auto_fix_escalate_cmd") or [])
        check("consider: escalate --reason embeds the repair history",
              "repair history" in _esc_reason, True)
        # Task D: >=2 DISTINCT models could not clear the SAME gate -> suspect-the-
        # grader cross-check is flagged in the human-read reason (L0,L1,L2 = 3).
        check("consider: >=2 models exhausted -> SUSPECT THE GRADER flagged",
              any("SUSPECT THE GRADER" in r for r in (_pjx.get("auto_fix_reasons") or [])),
              True)

        # (T1c) JUDGMENT failure SKIPS the ladder entirely (the tier boundary): a
        # reviewer-only code-high (no red verify) classifies escalate at the source,
        # so no fallback model is ever chosen -- it parks straight for Opus.
        _state([{"id": "jrJ", "auto_fix_root": "rootJ", "auto_fix_round": 0,
                 "job_cwd": str(_td), "job_verify": "bash verify.sh", "model": "L0"}])
        _pjj = {"verdict": "fail", "issues": [
            {"category": "code", "severity": "high", "source": "review",
             "what": "reviewer says bug"}]}
        autofix_consider("jrJ", _pjj, _td / "ollama-queue-logs" / "jrJ.gate.json",
                         _td / "ollama-queue-logs")
        check("consider: judgment (reviewer-code-high) escalates, skips ladder",
              (_pjj.get("auto_fix_action"), _pjj.get("auto_fix_class")),
              ("escalate", "reviewer-code-high"))
        check("consider: judgment escalate chose NO fallback model",
              _pjj.get("auto_fix_fallback_model"), None)
        check("consider: judgment escalate category is not persistent-nogo",
              _pjj.get("auto_fix_escalate_category") != "persistent-nogo", True)

        # (T1d) AUTHORING-JOB GUARD (2026-09-18): an `auto-author-*`/`auto-refine-*`
        # meta-task must NEVER escalate or requeue, even with an otherwise-escalating
        # dirty (untrusted) baseline -- the coding-gate findings are expected for a
        # harness author and the ollama-dispatch-auto orchestrator owns the refine
        # loop. Without the guard this exact payload escalated class=unattributable and
        # stranded the broker-guard slice batch (job aaf4dc13de08 et al.).
        _state([{"id": "jrA", "auto_fix_root": "rootA", "auto_fix_round": 0,
                 "label": "auto-author-bg-detection-s1-query-matrix", "cwd": str(_td),
                 "verify": "bash verify.sh", "model": "L0"}])
        _pja = {"verdict": "concerns",
                "job_label": "auto-author-bg-detection-s1-query-matrix",
                "untrusted": ["launch baseline was dirty (1 path(s))"],
                "issues": [{"severity": "high", "source": "launch-baseline",
                            "category": "input", "what": "dirty tree"}]}
        autofix_consider("jrA", _pja, _td / "ollama-queue-logs" / "jrA.gate.json",
                         _td / "ollama-queue-logs")
        check("consider: authoring job never escalates (orchestrator-owned)",
              (_pja.get("auto_fix_action"), _pja.get("auto_fix_class")),
              ("none", "authoring-orchestrator-owned"))
        check("consider: authoring job emits NO escalate command",
              _pja.get("auto_fix_escalate_cmd"), None)
        check("consider: authoring job emits NO requeue command",
              _pja.get("auto_fix_cmd_preview"), None)
        # (T1d2) PLAN-GEN GUARD (2026-10-05): a verify-red plan-gen round is NOT
        # requeued (ollama-dispatch-plan owns the round loop; a requeue raced r2 in
        # the same scratch dir -- f1cf7fa2ebab -> 95e0a836a3ef). Same payload with a
        # coding label still requeues (the guard is plan-gen-only).
        _state([{"id": "jrP", "auto_fix_root": "rootP", "auto_fix_round": 0,
                 "label": "plan-gen-rt-egift-link-s1-r1", "cwd": str(_td),
                 "verify": "bash verify.sh", "model": "L0"}])
        _pjp = {"verdict": "fail", "job_label": "plan-gen-rt-egift-link-s1-r1",
                "issues": [{"category": "code", "severity": "high", "source": "verify-exit",
                            "what": "the job's own verify FAILED (exit 1)"}],
                "job_exit_code": 1}
        _pjp_coding = json.loads(json.dumps(_pjp)); _pjp_coding["job_label"] = "rt-x-fix"
        autofix_consider("jrP", _pjp, _td / "ollama-queue-logs" / "jrP.gate.json",
                         _td / "ollama-queue-logs")
        check("consider: plan-gen round is never requeued (orchestrator-owned)",
              (_pjp.get("auto_fix_action"), _pjp.get("auto_fix_class"),
               _pjp.get("auto_fix_cmd_preview")),
              ("none", "plangen-orchestrator-owned", None))
        _state([{"id": "jrQ", "auto_fix_root": "rootQ", "auto_fix_round": 0,
                 "label": "rt-x-fix", "cwd": str(_td), "verify": "bash verify.sh",
                 "model": "L0"}])
        autofix_consider("jrQ", _pjp_coding, _td / "ollama-queue-logs" / "jrQ.gate.json",
                         _td / "ollama-queue-logs")
        check("consider: the same verify-red payload on a CODING label still requeues",
              _pjp_coding.get("auto_fix_action"), "requeue")
        # auto-refine-* is guarded identically.
        _pjar = dict(_pja); _pjar["job_label"] = "auto-refine-bg-eraser-s1-invoke-r1"
        autofix_consider("jrA", _pjar, _td / "ollama-queue-logs" / "jrAr.gate.json",
                         _td / "ollama-queue-logs")
        check("consider: auto-refine-* is guarded the same as auto-author-*",
              _pjar.get("auto_fix_action"), "none")
        # a REAL coding job with the same dirty baseline still escalates (guard is
        # authoring-only, not a blanket dirty-baseline waiver).
        _state([{"id": "jrC", "auto_fix_root": "rootC", "auto_fix_round": 0,
                 "label": "rt-costco-fix", "cwd": str(_td),
                 "verify": "bash verify.sh", "model": "L0"}])
        _pjc = {"verdict": "concerns", "job_label": "rt-costco-fix",
                "untrusted": ["launch baseline was dirty (1 path(s))"],
                "issues": [{"severity": "high", "source": "launch-baseline",
                            "category": "input", "what": "dirty tree"}]}
        autofix_consider("jrC", _pjc, _td / "ollama-queue-logs" / "jrC.gate.json",
                         _td / "ollama-queue-logs")
        check("consider: a real CODING job with a dirty baseline still escalates",
              _pjc.get("auto_fix_action"), "escalate")

        # restore the ladder env for the remaining (round-cap) integrated tests:
        # a single rung == the job model 'm', so 'm' exhausting has NO next rung and
        # the existing round-cap assertions (escalate at the cap) still hold.
        os.environ["OLLAMA_DISPATCH_MODEL_LADDER"] = "m"

        # --- SCOPE-REQUEUE GUARD (2026-09-17): a code-category reviewer finding
        # BELOW high, riding a scope-concerns verdict, must ESCALATE not requeue
        # (job 3a533bacd6d8: a MEDIUM review "deleted the login-confirmation
        # signal" slipped past the old `not code_high` guard). The requeue set is
        # scope-ONLY; a reviewer code opinion with no red verify is the false-FAIL
        # class the harness escalates. ---
        _scope_medcode = {"verdict": "concerns", "issues": [
            {"category": "input", "severity": "medium", "source": "scope",
             "what": "edits the verify that gated it"},
            {"category": "code", "severity": "medium", "source": "review",
             "what": "deleted the login-confirmation signal"}]}
        check("scope + medium code(review) finding -> escalate (not scope-requeue)",
              _cls(_scope_medcode), ("escalate", "undecidable"))
        # REVERT-TEST proving the guard BITES: the ONLY difference is the code
        # finding; strip it and the identical payload scope-requeues.
        _scope_only = {"verdict": "concerns", "issues": [
            {"category": "input", "severity": "medium", "source": "scope",
             "what": "edits the verify that gated it"}]}
        check("revert-test: same case WITHOUT the code finding -> scope-requeue",
              _cls(_scope_only), ("requeue", "scope"))

        # --- REQUEUE COMMAND BUILDER: the enqueue argv is the ONLY thing that
        # spends GPU in live mode. Assert it reuses the sealed --cwd/--verify,
        # carries the round/root markers, and writes the feedback task OUTSIDE the
        # sealed worktree (so the model cannot reach its own verify/task). ---
        _wt = _td / "wt-sealed"
        _wt.mkdir()
        (_wt / "TASK.md").write_text("Fix the bug in server.py\n")
        _state([{"id": "rq1", "status": "done", "exit_code": 1,
                 "auto_fix_root": "rq1", "cwd": str(_wt), "verify": "bash verify.sh",
                 "model": "qwen3.8:27b-q4_K_M", "host_pref": "studio",
                 "num_ctx": 32768, "max_iters": 40, "task_kind": "coding",
                 "label": "myfix", "task_file": str(_wt / "TASK.md")}])
        _argv, _fb = autofix_build_requeue(
            "rq1", {"issues": []}, _td / "ollama-queue-logs", 1, "rq1",
            {"class": "verify-red", "reasons": ["verify exit 1"]})

        def _flag(v, name):
            return v[v.index(name) + 1] if name in v else None
        check("requeue argv reuses the sealed --cwd (same worktree)",
              _flag(_argv, "--cwd"), str(_wt))
        check("requeue argv reuses the sealed --verify",
              _flag(_argv, "--verify"), "bash verify.sh")
        check("requeue argv carries the same model",
              _flag(_argv, "--model"), "qwen3.8:27b-q4_K_M")
        check("requeue argv carries --host from host_pref",
              _flag(_argv, "--host"), "studio")
        check("requeue argv carries --auto-fix-round N",
              _flag(_argv, "--auto-fix-round"), "1")
        check("requeue argv carries --auto-fix-root",
              _flag(_argv, "--auto-fix-root"), "rq1")
        check("requeue label gets the [auto-fix rN] marker",
              _flag(_argv, "--label"), "myfix [auto-fix r1]")
        check("requeue --task-file is OUTSIDE the sealed worktree",
              _flag(_argv, "--task-file") is not None
              and str(_wt) not in _flag(_argv, "--task-file"), True)
        check("feedback task file preserves the ORIGINAL task text",
              "Fix the bug in server.py" in _fb.read_text(), True)
        check("feedback task file appends the gate-feedback section",
              "Gate feedback from previous attempt" in _fb.read_text(), True)
        # label marker must NOT accrete across rounds: an already-marked label
        # gets its old marker stripped before the new one is appended.
        _argv2, _ = autofix_build_requeue(
            "rq1", {"issues": []}, _td / "ollama-queue-logs", 2, "rq1",
            {"class": "verify-red", "reasons": ["x"]})
        _state([{"id": "rq1", "status": "done", "exit_code": 1, "cwd": str(_wt),
                 "verify": "bash verify.sh", "model": "m",
                 "label": "myfix [auto-fix r1]", "task_file": str(_wt / "TASK.md")}])
        _argv3, _ = autofix_build_requeue(
            "rq1", {"issues": []}, _td / "ollama-queue-logs", 2, "rq1",
            {"class": "verify-red", "reasons": ["x"]})
        check("requeue label marker does not accrete (r1 stripped before r2)",
              _flag(_argv3, "--label"), "myfix [auto-fix r2]")
        # Audit 2026-09-23: a job record with NO readable task_file (old sidecar,
        # or a torn row) must still carry the ORIGINAL task text by falling back
        # to <cwd>/TASK.md -- the old fallback was payload["job_cwd"], a
        # directory, so the requeue's task file was feedback-only.
        _wt2 = _td / "wt-notaskfile"
        _wt2.mkdir()
        (_wt2 / "TASK.md").write_text("Fix the off-by-one in pager.py\n")
        _state([{"id": "rq2", "status": "done", "exit_code": 1, "cwd": str(_wt2),
                 "verify": "bash verify.sh", "model": "m", "label": "nofile"}])
        _argv4, _fb4 = autofix_build_requeue(
            "rq2", {"issues": [], "job_cwd": str(_wt2)}, _td / "ollama-queue-logs",
            1, "rq2", {"class": "verify-red", "reasons": ["x"]})
        check("requeue with no task_file on the record falls back to <cwd>/TASK.md",
              "Fix the off-by-one in pager.py" in _fb4.read_text(), True)
        check("...and still appends the gate-feedback section",
              "Gate feedback from previous attempt" in _fb4.read_text(), True)

        # --- LOOP GUARD 1 (round cap): a would-be requeue whose next round would
        # exceed AUTOFIX_MAX_ROUNDS escalates instead. REVERT-TEST: one round
        # below the cap, the SAME verify-red requeues -- proving the cap is what
        # bit, not something else. ---
        def _vr():
            return {"verdict": "fail", "counts": {"total": 1}, "issues": [
                {"category": "code", "severity": "high", "source": "verify-exit",
                 "file": "s.py", "what": "exit 1"}]}
        _cap = AUTOFIX_MAX_ROUNDS
        _state([{"id": "cap1", "status": "done", "exit_code": 1,
                 "auto_fix_root": "capR", "auto_fix_round": _cap,
                 "cwd": str(_wt), "verify": "bash verify.sh", "model": "m",
                 "label": f"x [auto-fix r{_cap}]", "task_file": str(_wt / "TASK.md")}])
        _pc1 = _vr()
        autofix_consider("cap1", _pc1, _td / "ollama-queue-logs" / "cap1.gate.json",
                         _td / "ollama-queue-logs")
        check("round-cap: a requeue past the cap -> escalate",
              _pc1.get("auto_fix_action"), "escalate")
        check("round-cap: escalation reason names the cap",
              any("cap" in str(r) for r in _pc1.get("auto_fix_reasons", [])), True)
        check("round-cap: no requeue command emitted at the cap",
              _pc1.get("auto_fix_cmd_preview"), None)
        _state([{"id": "cap0", "status": "done", "exit_code": 1,
                 "auto_fix_root": "capR0", "auto_fix_round": _cap - 1,
                 "cwd": str(_wt), "verify": "bash verify.sh", "model": "m",
                 "label": f"x [auto-fix r{_cap - 1}]", "task_file": str(_wt / "TASK.md")}])
        _pc0 = _vr()
        autofix_consider("cap0", _pc0, _td / "ollama-queue-logs" / "cap0.gate.json",
                         _td / "ollama-queue-logs")
        check("round-cap REVERT-TEST: one round below the cap -> requeue",
              _pc0.get("auto_fix_action"), "requeue")

        # --- CLEAN never spends GPU: a pass verdict at the consider level records
        # action=none and emits NO requeue command, in any mode. ---
        _pcl = {"verdict": "pass", "issues": []}
        autofix_consider("clean1", _pcl,
                         _td / "ollama-queue-logs" / "clean1.gate.json",
                         _td / "ollama-queue-logs")
        check("consider: clean pass -> action none",
              _pcl.get("auto_fix_action"), "none")
        check("consider: clean pass emits NO requeue command",
              _pcl.get("auto_fix_cmd_preview"), None)

        # --- COMPLETED-CODE-DROP: verdict-keyed, browsable, idempotent ------
        _ld = _td / "ollama-queue-logs"

        def _mkdrop(jid, label, verdict, rel="src/app.ts", body="ADDED\n"):
            """Fixture: a worktree with one changed file + the job's .diff sidecar
            in LOG_DIR, exactly as gate time sees them; returns the drop payload."""
            wt = _td / f"wt-{jid}"
            (wt / Path(rel).parent).mkdir(parents=True, exist_ok=True)
            (wt / rel).write_text(body)
            (_ld / f"{jid}.diff").write_text(
                f"diff --git a/{rel} b/{rel}\n--- a/{rel}\n+++ b/{rel}\n"
                f"@@ -0,0 +1 @@\n+{body.strip()}\n")
            return {"verdict": verdict, "job_label": label, "job_cwd": str(wt),
                    "job_model": "qwen3.8:27b-q4_K_M", "job_host": "studio",
                    "task_intent": "make the thing work", "ts": "2026-09-17T00:00:00Z"}

        # (1) PASS -> Pass/<label>-<shortid>/ with the diff, files/, SUMMARY.md.
        _pp = _mkdrop("aaaa11112222", "pass-job", "pass")
        _pdest = drop_completed_code("aaaa11112222", _pp, _ld)
        check("pass drop lands under Pass/",
              _pdest is not None and _pdest.parent.name == "Pass", True)
        check("pass drop folder is <label>-<shortid>",
              _pdest.name if _pdest else None, "pass-job-aaaa11112222")
        check("pass drop has the diff",
              (_pdest / "aaaa11112222.diff").exists() if _pdest else False, True)
        check("pass drop captured the changed file post-change",
              (_pdest / "files" / "src/app.ts").read_text() if _pdest else "", "ADDED\n")
        check("pass drop SUMMARY records verdict + model",
              _pdest is not None and "Verdict:** pass" in (_pdest / "SUMMARY.md").read_text()
              and "qwen3.8:27b-q4_K_M" in (_pdest / "SUMMARY.md").read_text(), True)

        # (2) FAIL (any non-pass terminal verdict) -> Failed/.
        _fp = _mkdrop("bbbb33334444", "fail-job", "fail")
        _fdest = drop_completed_code("bbbb33334444", _fp, _ld)
        check("fail drop lands under Failed/",
              _fdest is not None and _fdest.parent.name == "Failed", True)
        check("concerns verdict also lands under Failed/ (non-pass)",
              (drop_completed_code("cccc55556666",
                  _mkdrop("cccc55556666", "concerns-job", "concerns"), _ld)
               ).parent.name, "Failed")

        # (3) NON-TERMINAL / nothing-to-drop verdicts return None, write nothing.
        check("pass-pending-review does NOT drop (non-terminal)",
              drop_completed_code("dddd7777",
                  {"verdict": "pass-pending-review", "job_label": "x",
                   "job_cwd": str(_td)}, _ld), None)
        check("skipped (no diff) does NOT drop",
              drop_completed_code("eeee8888",
                  {"verdict": "skipped", "job_label": "x"}, _ld), None)

        # (4) IDEMPOTENT: re-running the same PASS drop does not duplicate or crash.
        drop_completed_code("aaaa11112222", _pp, _ld)
        check("idempotent: exactly one Pass folder for the job after a re-run",
              len(list((COMPLETED_ROOT / "Pass").glob("*-aaaa11112222"))), 1)

        # (5) SINGLE-BUCKET: a later verdict for the SAME job moves the folder and
        # leaves no stale copy in the other bucket (pre-gate -> authoritative regate).
        _pp2 = _mkdrop("aaaa11112222", "pass-job", "fail")
        drop_completed_code("aaaa11112222", _pp2, _ld)
        check("verdict flip moves the drop to Failed/",
              len(list((COMPLETED_ROOT / "Failed").glob("*-aaaa11112222"))), 1)
        check("verdict flip leaves NO stale drop in Pass/",
              len(list((COMPLETED_ROOT / "Pass").glob("*-aaaa11112222"))), 0)

        # (6) worktree GONE (reaped): the drop still lands with the diff alone.
        _gp = {"verdict": "pass", "job_label": "reaped-job",
               "job_cwd": str(_td / "does-not-exist"),
               "job_model": "m", "job_host": "h"}
        (_ld / "ffff9999.diff").write_text("diff --git a/x b/x\n+++ b/x\n@@\n+y\n")
        _gdest = drop_completed_code("ffff9999", _gp, _ld)
        check("reaped worktree still drops the diff (diff-only)",
              _gdest is not None and (_gdest / "ffff9999.diff").exists()
              and not (_gdest / "files").exists(), True)

        # (7) ADVISORY INVARIANT: drop never raises on garbage input.
        _draised = False
        try:
            drop_completed_code("bad", {"verdict": object()}, _ld)
        except Exception:
            _draised = True
        check("drop_completed_code swallows all errors (never raises)", _draised, False)

        # --- ADVISORY INVARIANT: autofix_consider never raises, even on garbage
        # input (rule 2: the gate must never fail the job). ---
        _raised = False
        try:
            autofix_consider("bad", {"verdict": object()},  # unserialisable
                             _td / "ollama-queue-logs" / "bad.gate.json",
                             _td / "ollama-queue-logs")
        except Exception:
            _raised = True
        check("autofix_consider swallows all errors (never raises)", _raised, False)

        # ===================== END-TO-END AUTO-PIPELINE (Task C) =============
        # (A) apply_policy: auto-deploy repo -> auto-apply; else / unknown -> ready.
        check("C policy: resell-tracker -> auto-apply", apply_policy("resell-tracker"), "auto-apply")
        check("C policy: other repo -> ready", apply_policy("sonarr"), "ready")
        check("C policy: unknown/None -> ready (fail-safe)", apply_policy(None), "ready")

        # (B) classify_advance -- the pure success-path decision.
        _clean_proven = {"verdict": "pass", "issues": [],
                         "verify_relevance": {"verdict": "relevant", "survivors": []}}
        check("C advance: authoring CLEAN+PROVEN -> advance-to-coding",
              classify_advance(_clean_proven, "auto-author-foo")["action"], "advance-to-coding")
        check("C advance: authoring relevance UNPROVEN -> none (refine owns it)",
              classify_advance({"verdict": "pass", "issues": [],
                                "verify_relevance": {"verdict": "unproven", "survivors": []}},
                               "auto-refine-foo-r2")["action"], "none")
        check("C advance: authoring PROVEN but SURVIVORS>0 -> none",
              classify_advance({"verdict": "pass", "issues": [],
                                "verify_relevance": {"verdict": "relevant",
                                                     "survivors": [{"x": 1}]}},
                               "auto-author-foo")["action"], "none")
        check("C advance: authoring gate CONCERNS -> none (failure ladder owns it)",
              classify_advance({"verdict": "concerns", "issues": [],
                                "verify_relevance": {"verdict": "relevant", "survivors": []}},
                               "auto-author-foo")["action"], "none")
        check("C advance: authoring FAIL -> none",
              classify_advance({"verdict": "fail",
                                "verify_relevance": {"verdict": "relevant", "survivors": []}},
                               "auto-author-foo")["action"], "none")
        # coding jobs: relevance is NOT re-required; a clean PASS applies per policy.
        # MACHINE-PROVEN clean pass (2026-10-06): both-ways proof, relevance
        # relevant/0 survivors, plain reviewer PASS, judged stamp -> auto-apply.
        _proven_pass = {"verdict": "pass", "issues": [], "review_verdict": "PASS",
                        "verify_failed_at_baseline": True,
                        "verify_relevance": {"verdict": "relevant", "survivors": [],
                                             "killed": 5, "survived": 0},
                        "judged": {"files": {"app/x.ts": "h"}, "toplevel": "/x"}}
        check("C apply: coding MACHINE-PROVEN PASS (auto-deploy repo) -> apply/auto-apply",
              (lambda d: (d["action"], d["policy"], d["evidence_gaps"]))(
                  classify_advance(_proven_pass, "rt-costco", "resell-tracker")),
              ("apply", "auto-apply", []))
        # A PASS resting on the reviewer's word alone is still landable, but by a
        # HUMAN: auto-apply downgrades to ready and the gaps are named.
        check("C apply: coding CLEAN but UNPROVEN PASS (auto-deploy repo) -> apply/ready",
              (lambda d: (d["action"], d["policy"]))(
                  classify_advance({"verdict": "pass", "issues": []}, "rt-costco", "resell-tracker")),
              ("apply", "ready"))
        for _gk, _gv, _gwhy in (
                ("verify_failed_at_baseline", False, "both-ways"),
                ("verify_relevance", {"verdict": "unproven", "survivors": []}, "relevance not proven"),
                ("verify_relevance", {"verdict": "relevant", "survivors": [{"file": "a"}]}, "survivor"),
                ("verify_relevance", {"verdict": "relevant", "survivors": [],
                                      "optout_rejected": [{"line": 3}]}, "REJECTED"),
                ("verify_relevance", {"verdict": "relevant", "survivors": [],
                                      "source_text_harness": {"verdict": "mixed"}}, "SOURCE TEXT"),
                ("review_verdict", "PASS WITH CAVEATS", "not a plain PASS"),
                ("second_opinion_disagreement", True, "DISAGREES"),
                ("judged", {}, "judged-identity")):
            _d = classify_advance(dict(_proven_pass, **{_gk: _gv}), "rt-costco", "resell-tracker")
            check(f"C apply: evidence gap {_gk}={str(_gv)[:30]} -> ready, gap named",
                  (_d["policy"], any(_gwhy in g for g in _d["evidence_gaps"])), ("ready", True))
        check("C apply: coding CLEAN PASS (other repo) -> apply/ready",
              classify_advance({"verdict": "pass", "issues": []}, "arr-x", "sonarr")["policy"], "ready")
        check("C apply: coding with a code finding -> none",
              classify_advance({"verdict": "pass",
                                "issues": [{"category": "code", "severity": "high"}]},
                               "arr-x", "sonarr")["action"], "none")
        check("C apply: coding CONCERNS -> none",
              classify_advance({"verdict": "concerns", "issues": []}, "arr-x")["action"], "none")
        check("C clean: dirty (untrusted) baseline is never clean",
              _gate_is_clean({"verdict": "pass", "untrusted": ["dirty"]}), False)

        # --- SLICE AUTO-FEED decision (2026-09-18) ----------------------------
        # "one job creates the next": a completed slice job fires its own plan's
        # advance. PURE core, so the exact logic the daemon runs is tested here.
        _runs = [{"label": "alpha", "plan_path": "/p/alpha.json",
                  "slices": {"s1": {}, "s2": {}}},
                 {"label": "alpha-extra", "plan_path": "/p/extra.json",
                  "slices": {"s1": {}}}]
        check("S base: the authoring wrapper is stripped to the slice label",
              (_slice_base_label("auto-author-alpha-s2"),
               _slice_base_label("auto-refine-alpha-s2-r3"),
               _slice_base_label("alpha-s2")), ("alpha-s2",) * 3)
        check("S owner: a coding job maps to its plan",
              (slice_run_for_label("alpha-s2", _runs) or {}).get("label"), "alpha")
        check("S owner: a gate AUTO-FIX round ('<label> [auto-fix rN]') maps to the "
              "same plan (01fdda651fba was 'does not belong to any slice run')",
              ((slice_run_for_label("alpha-s2 [auto-fix r1]", _runs) or {}).get("label"),
               (slice_run_for_label("auto-author-alpha-s1 [auto-fix r3]", _runs) or {})
               .get("label"),
               slice_autofeed_decision("alpha-s2 [auto-fix r2]", "pass", {}, _runs)[::2]),
              ("alpha", "alpha", ("advance", "/p/alpha.json")))
        check("S owner: an AUTHORING job maps to the same plan",
              (slice_run_for_label("auto-author-alpha-s1", _runs) or {}).get("label"),
              "alpha")
        check("S owner: the LONGEST matching plan label wins (no prefix theft)",
              (slice_run_for_label("alpha-extra-s1", _runs) or {}).get("label"),
              "alpha-extra")
        check("S owner: a label whose slice id the plan never declared is NOT owned",
              slice_run_for_label("alpha-s9", _runs), None)
        check("S owner: an unrelated dispatch is NOT owned",
              slice_run_for_label("rt-costco-fix", _runs), None)
        check("S feed: a PASSING slice coding job advances its plan",
              slice_autofeed_decision("alpha-s2", "pass", {}, _runs)[::2],
              ("advance", "/p/alpha.json"))
        check("S feed: a PASSING slice AUTHOR job advances too "
              "(author -> code, no manual --execute)",
              slice_autofeed_decision("auto-author-alpha-s1", "pass", {}, _runs)[0],
              "advance")
        # Cause-2b (2026-09-21): a RED slice job must no longer be dropped -- it is
        # RE-DISPATCHED so the slicer's bounded-retry/escalate runs (was "skip").
        check("S feed: CONCERNS on a coding job -> redispatch (was skip; never park dead)",
              slice_autofeed_decision("alpha-s2", "concerns", {}, _runs)[::2],
              ("redispatch", "/p/alpha.json"))
        check("S feed: FAIL -> redispatch (bounded retry -> needs_opus)",
              slice_autofeed_decision("alpha-s2", "fail", {}, _runs)[0], "redispatch")
        check("S feed: nonconvergence/error -> redispatch too",
              (slice_autofeed_decision("alpha-s2", "nonconvergence", {}, _runs)[0],
               slice_autofeed_decision("alpha-s2", "error", {}, _runs)[0]),
              ("redispatch", "redispatch"))
        check("S feed: pass-pending-review is an INTERMEDIATE state -> still skip "
              "(never redispatched, preserved behaviour)",
              slice_autofeed_decision("alpha-s2", "pass-pending-review", {}, _runs)[0],
              "skip")
        # Cause-2a (2026-09-21): an authoring/refine job whose VERIFY is GREEN (exit 0)
        # but whose verdict was demoted to `concerns` (the dirty-baseline false-fail)
        # AUTO-LANDS rather than parking failed.
        check("S feed 2a: auto-refine, verify GREEN (exit 0), concerns -> ADVANCE (auto-land)",
              slice_autofeed_decision("auto-refine-alpha-s2-r3", "concerns",
                                      {"exit_code": 0}, _runs)[0], "advance")
        check("S feed 2a: a GENUINE nonconvergence (exit != 0) does NOT auto-land -> redispatch",
              slice_autofeed_decision("auto-refine-alpha-s2-r3", "concerns",
                                      {"exit_code": 1}, _runs)[0], "redispatch")
        check("S feed 2a: the carve-out is authoring-only -- a CODING concerns with exit 0 "
              "still redispatches (its landing is the ENQUEUED-poll path, not this)",
              slice_autofeed_decision("alpha-s2", "concerns", {"exit_code": 0}, _runs)[0],
              "redispatch")
        check("S feed 2a: authoring green but a FAIL (code-high) verdict does NOT land",
              slice_autofeed_decision("auto-refine-alpha-s2-r3", "fail",
                                      {"exit_code": 0}, _runs)[0], "redispatch")
        # a72d4344's contract, kept intact: bonsai (research + --runner) is never
        # gated and must never be auto-fed a coding job.
        check("S feed: a RESEARCH job is never auto-fed",
              slice_autofeed_decision("alpha-s2", "pass",
                                      {"task_kind": "research"}, _runs)[0], "skip")
        check("S feed: an alternate --runner job is never auto-fed",
              slice_autofeed_decision("alpha-s2", "pass",
                                      {"runner": "bakeoff-runner.py"}, _runs)[0], "skip")
        check("S feed: a non-slice dispatch is never auto-fed",
              slice_autofeed_decision("rt-costco-fix", "pass", {}, _runs)[0], "skip")
        check("S feed: a plan with no recorded plan_path cannot advance",
              slice_autofeed_decision(
                  "beta-s1", "pass", {},
                  [{"label": "beta", "slices": {"s1": {}}}])[0], "skip")
        check("C clean: pass-pending-review is NOT terminal-clean",
              _gate_is_clean({"verdict": "pass-pending-review", "issues": []}), False)

        # (C) SHADOW mode (default, switch OFF): auto_pipeline_consider RECORDS the
        # decision but performs NOTHING -- no advance/apply marker is set. This is
        # the "staged behind a clear switch" proof.
        check("C switch: default mode is shadow (OFF)", AUTO_PIPELINE_MODE, "shadow")
        # a COMPLETE sealed harness in the worktree (TASK.md + verify.sh present).
        (_td / "TASK.md").write_text("do the thing\n")
        (_td / "verify.sh").write_text("exit 0\n")
        _gj = _td / "ollama-queue-logs" / "cadv.gate.json"
        _pl = dict(_clean_proven); _pl["job_label"] = "auto-author-bar"; _pl["cwd"] = str(_td)
        auto_pipeline_consider("cadv", _pl, _gj)
        _rec = json.loads(_gj.read_text())
        check("C shadow: records the advance decision",
              _rec.get("auto_pipeline_action"), "advance-to-coding")
        check("C shadow: does NOT actually advance (no marker set)",
              _rec.get("auto_pipeline_advanced"), None)
        # 2026-09-17: the shadow preview is now downstream of the RISK TIER. This
        # tree has no fixture for the automated relevance pass to certify, so the
        # tier holds it for a human and there is deliberately NO preview -- a
        # preview of an auto-seal the live path would refuse is a preview of the
        # wrong system.
        check("C shadow: no-fixture tree is HELD for a human, not previewed",
              (_rec.get("auto_confirm_tier"), _rec.get("auto_pipeline_status")),
              ("human", "held-for-human-confirm"))
        check("C shadow: a held tree records NO command preview",
              _rec.get("auto_pipeline_advance_preview"), None)
        # ...and the other half: a LOW-risk tree (benign task, faithful fixture,
        # lint present) still previews exactly as before. Without this the tier
        # could be refusing everything and the suite would look green.
        _lowwt = _td / "wt_shadow_low"
        _lowwt.mkdir(parents=True, exist_ok=True)
        (_lowwt / "TASK.md").write_text(
            "Fix the codec rank ordering in ranker.py so a higher tier never "
            "sorts below a lower one. Only edit ranker.py.\n")
        (_lowwt / "verify.sh").write_text("exit 0\n")
        (_lowwt / "test_fixture.py").write_text(_SELFTEST_FAITHFUL_FIXTURE)
        if (_orig_bin / "faithful-harness-lint.py").exists():
            _sh.copy2(_orig_bin / "faithful-harness-lint.py",
                      _td / "faithful-harness-lint.py")
            _gjl = _td / "ollama-queue-logs" / "cadvlow.gate.json"
            _pll = dict(_clean_proven)
            _pll["job_label"] = "auto-author-low"; _pll["cwd"] = str(_lowwt)
            auto_pipeline_consider("cadvlow", _pll, _gjl)
            _recl = json.loads(_gjl.read_text())
            check("C shadow: LOW-risk tree is previewed (tier discriminates)",
                  (_recl.get("auto_confirm_tier"),
                   bool(_recl.get("auto_pipeline_advance_preview"))),
                  ("low", True))
        # HALT+SURFACE: advance on an INCOMPLETE harness (no verify.sh) parks/records
        # the failure and never sets the advanced marker.
        _gjh = _td / "ollama-queue-logs" / "chalt.gate.json"
        _emptywt = _td / "emptywt"; _emptywt.mkdir()
        advance_to_coding("chalt", {"cwd": str(_emptywt), "job_label": "auto-author-z"}, _gjh)
        _rh = json.loads(_gjh.read_text())
        check("C halt: incomplete harness -> advance-failed, not advanced",
              (_rh.get("auto_pipeline_status"), _rh.get("auto_pipeline_advanced")),
              ("advance-failed", None))

        # (D) idempotency: an already-advanced job is a no-op.
        _gj2 = _td / "ollama-queue-logs" / "cidem.gate.json"
        _pl2 = {"auto_pipeline_advanced": True, "cwd": str(_td), "job_label": "auto-author-x"}
        advance_to_coding("cidem", _pl2, _gj2)
        check("C idempotent: advance is a no-op once auto_pipeline_advanced is set",
              _gj2.exists(), False)

        # (E) GUARDRAIL: the apply step's target set EXCLUDES every scaffold/harness
        # file -- it can only ever copy TARGET code, never verify/fixture/check.
        _targets = _apply_target_files(
            {"changed_files": ["app/main.py", "verify.sh", "test_fixture.py",
                               "check_literals.py", "src/lib/util.ts", "refimpl.py",
                               "TASK.md", "auto-harness-check.py"]}, "t")
        check("C guardrail: apply targets are TARGET code only",
              sorted(_targets), ["app/main.py", "src/lib/util.ts"])

        # (F) apply_fix policy behaviour (shadow): 'ready' marks ready-to-apply and
        # copies nothing; 'auto-apply' in shadow only writes a preview.
        _gjr = _td / "ollama-queue-logs" / "cready.gate.json"
        _pr = {"changed_files": ["app/main.py"], "cwd": str(_td)}
        apply_fix("cready", _pr, _gjr, "ready")
        _rr = json.loads(_gjr.read_text())
        check("C apply/ready: marks ready-to-apply", _rr.get("auto_pipeline_status"), "ready-to-apply")
        check("C apply/ready: records the target list", _rr.get("auto_pipeline_apply_targets"), ["app/main.py"])
        _gja = _td / "ollama-queue-logs" / "cauto.gate.json"
        _pa = {"changed_files": ["app/main.py"], "cwd": str(_td)}
        apply_fix("cauto", _pa, _gja, "auto-apply")
        _ra = json.loads(_gja.read_text())
        check("C apply/auto (shadow): does NOT apply (no applied marker)",
              _ra.get("auto_pipeline_applied"), None)
        check("C apply/auto (shadow): records an apply preview",
              bool(_ra.get("auto_pipeline_apply_preview")), True)

        # (F2) LIVE auto-apply over a real git worktree: clean main -> copies TARGET
        # to main; dirty main -> PARKS (no clobber); --subdir source resolves via
        # the worktree toplevel. Needs git; skipped cleanly if unavailable.
        def _git(*args, cwd=None):
            return subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                                  capture_output=True, text=True, timeout=60,
                                  env={**os.environ, "GIT_AUTHOR_NAME": "t",
                                       "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                                       "GIT_COMMITTER_EMAIL": "t@t"})
        _have_git = False
        try:
            _have_git = _git("--version").returncode == 0
        except Exception:
            _have_git = False
        if _have_git:
            _saved_mode = AUTO_PIPELINE_MODE
            AUTO_PIPELINE_MODE = "live"
            # HERMETIC (2026-10-06): a live-mode PARK runs `ollama-queue.py escalate
            # <id>` against the REAL queue. These fixture ids (cdirty, cstale, ...)
            # are not real jobs, but a self-test must never call the live queue at
            # all -- record the argv instead.
            _saved_esc = globals()["autofix_build_escalate"]
            _esc_calls = []
            globals()["autofix_build_escalate"] = (
                lambda *a, **k: (_esc_calls.append(a[0]) or ["true"]))
            try:
                _repo = _td / "origin-repo"; _repo.mkdir()
                _git("init", "-q", cwd=_repo)
                (_repo / "app").mkdir()
                (_repo / "app" / "main.py").write_text("ORIGINAL\n")
                _git("add", "-A", cwd=_repo); _git("commit", "-q", "-m", "init", cwd=_repo)
                _wt = _td / "wt-x"
                _git("worktree", "add", "-q", str(_wt), cwd=_repo)
                (_wt / "app" / "main.py").write_text("FIXED\n")  # the "fix" in the worktree
                # clean-main apply -> copies FIXED to origin main tree.
                _gjc = _td / "ollama-queue-logs" / "capply.gate.json"
                def _stamp(rel, top=_wt):
                    return {"files": {rel: _gate_identity()._sha(top / rel)},
                            "toplevel": str(top)}
                # VERDICT IDENTITY: an UNSTAMPED verdict is refused (fail closed),
                # and a stamp whose file changed after the verdict is STALE.
                _gjn = _td / "ollama-queue-logs" / "cnostamp.gate.json"
                apply_fix("cnostamp", {"changed_files": ["app/main.py"], "cwd": str(_wt)},
                          _gjn, "auto-apply")
                check("C apply/live: UNSTAMPED verdict -> PARKS, main untouched",
                      (json.loads(_gjn.read_text()).get("auto_pipeline_status"),
                       (_repo / "app" / "main.py").read_text()), ("apply-failed", "ORIGINAL\n"))
                _stale = _stamp("app/main.py")
                (_wt / "app" / "main.py").write_text("EDITED-AFTER-GATE\n")
                _gjst = _td / "ollama-queue-logs" / "cstale.gate.json"
                apply_fix("cstale", {"changed_files": ["app/main.py"], "cwd": str(_wt),
                                     "judged": _stale}, _gjst, "auto-apply")
                _rst = json.loads(_gjst.read_text())
                check("C apply/live: STALE verdict (file changed after gate) -> PARKS",
                      (_rst.get("auto_pipeline_status"),
                       "STALE" in json.dumps(_rst), (_repo / "app" / "main.py").read_text()),
                      ("apply-failed", True, "ORIGINAL\n"))
                (_wt / "app" / "main.py").write_text("FIXED\n")
                _pc = {"changed_files": ["app/main.py"], "cwd": str(_wt),
                       "judged": _stamp("app/main.py")}
                apply_fix("capply", _pc, _gjc, "auto-apply")
                _rc2 = json.loads(_gjc.read_text())
                check("C apply/live: clean main -> applied-to-main",
                      _rc2.get("auto_pipeline_status"), "applied-to-main")
                check("C apply/live: TARGET copied to main working tree",
                      (_repo / "app" / "main.py").read_text(), "FIXED\n")
                # dirty-main guard: make main's target dirty, retry with a fresh job.
                (_repo / "app" / "main.py").write_text("HUMAN-WIP\n")  # uncommitted edit
                (_wt / "app" / "main.py").write_text("FIXED2\n")
                _gjd = _td / "ollama-queue-logs" / "cdirty.gate.json"
                _pd = {"changed_files": ["app/main.py"], "cwd": str(_wt),
                       "judged": _stamp("app/main.py")}
                apply_fix("cdirty", _pd, _gjd, "auto-apply")
                _rd2 = json.loads(_gjd.read_text())
                check("C apply/live: dirty main -> PARKS (apply-failed), no clobber",
                      _rd2.get("auto_pipeline_status"), "apply-failed")
                check("C apply/live: dirty main file left UNTOUCHED",
                      (_repo / "app" / "main.py").read_text(), "HUMAN-WIP\n")
                check("C dirty-guard: fail-closed on a dirty path",
                      _target_dirty_in_main(_repo, "app/main.py"), True)
                # --subdir source resolution: cwd is a subdir, target still resolves
                # from the worktree toplevel (not cwd/subdir/app/main.py).
                (_repo / "app" / "main.py").write_text("ORIGINAL\n")  # clean it
                _git("add", "-A", cwd=_repo); _git("commit", "-q", "-m", "reset", cwd=_repo)
                (_wt / "app" / "main.py").write_text("SUBDIRFIX\n")
                _subdir = _wt / "app"  # a --subdir cwd
                _gjs = _td / "ollama-queue-logs" / "csub.gate.json"
                _ps = {"changed_files": ["app/main.py"], "cwd": str(_subdir),
                       "judged": _stamp("app/main.py")}
                apply_fix("csub", _ps, _gjs, "auto-apply")
                check("C apply/live: --subdir job resolves target via toplevel",
                      (_repo / "app" / "main.py").read_text(), "SUBDIRFIX\n")
                check("C apply/live: parks escalated via the STUB, never the live queue",
                      sorted(_esc_calls), ["cdirty", "cnostamp", "cstale"])
            finally:
                AUTO_PIPELINE_MODE = _saved_mode
                globals()["autofix_build_escalate"] = _saved_esc

        # (G) advisory invariant: auto_pipeline_consider never raises on garbage.
        _apr = False
        try:
            auto_pipeline_consider("bad2", {"verdict": object()},
                                   _td / "ollama-queue-logs" / "bad2.gate.json")
        except Exception:
            _apr = True
        check("C: auto_pipeline_consider swallows all errors (never raises)", _apr, False)

        # ============================================================
        # RUN-STATUS JANITOR (daemon auto-archive of no-eyes-needed rows)
        # ============================================================
        def _jc(**kw):
            base = dict(label="fix-thing", cwd="/tmp/x", verdict="pass",
                        status="done", awaiting_signoff=False, is_dead_slice=False)
            base.update(kw)
            return classify_runstatus_janitor(**base)
        # AUTO_ARCHIVE categories
        check("janitor: eval/probe label -> auto-archive (eval-arm)",
              _jc(label="clamp-adequacy-3")["category"], "eval-arm")
        check("janitor: probe cwd -> auto-archive",
              _jc(label="anything", cwd="/Users/example/r3probe-clamp")["action"],
              "auto-archive")
        check("janitor: capability- label -> auto-archive",
              _jc(label="capability-probe-tools")["action"], "auto-archive")
        check("janitor: dead slice fragment -> auto-archive (dead-slice)",
              _jc(label="bfmrAutoLink-s3-if-it-does", status="failed",
                  is_dead_slice=True)["category"], "dead-slice")
        # SURFACE categories (must reach a human) -- checked with PRIORITY
        check("janitor: PASS awaiting sign-off -> SURFACE",
              _jc(verdict="pass", awaiting_signoff=True)["category"],
              "awaiting-signoff")
        check("janitor: CONCERNS on a real app repo -> SURFACE",
              _jc(verdict="concerns",
                  cwd=_GH_PROJECTS_ROOT + "/resell-tracker")["category"],
              "real-repo-non-pass")
        check("janitor: FAIL on a real app repo -> SURFACE",
              _jc(verdict="fail",
                  cwd=_GH_PROJECTS_ROOT + "/travel-esim-rates")["action"], "surface")
        check("janitor: live (non-terminal) refine round -> SURFACE (live)",
              _jc(status="running")["category"], "live")
        check("janitor: ordinary clean deliverable -> SURFACE (fail-safe)",
              _jc(label="fix-order-919",
                  cwd=_GH_PROJECTS_ROOT + "/resell-tracker")["action"], "surface")
        # a non-pass in a SCRATCH/eval tree is NOT a real-repo defect: eval rule wins
        check("janitor: CONCERNS in a bakeoff/scratch tree -> auto-archive",
              _jc(label="bo-arm", verdict="concerns",
                  cwd="/tmp/bakeoff/wt")["action"], "auto-archive")
        # awaiting sign-off OUTRANKS an eval-looking label (sign-off checked first)
        check("janitor: sign-off owed beats eval-looking label -> SURFACE",
              _jc(label="clamp-x", awaiting_signoff=True)["action"], "surface")
        # real-app-repo predicate
        check("janitor: worktree cwd is NOT a real app repo",
              _is_real_app_repo("/Users/example/.ollama-dispatch/worktrees/wt-x"), False)
        check("janitor: GitHub Projects checkout IS a real app repo",
              _is_real_app_repo(_GH_PROJECTS_ROOT + "/resell-tracker"), True)
        check("janitor: resolved GitHub Projects.nosync checkout IS a real app repo",
              _is_real_app_repo(_GH_PROJECTS_ROOT + ".nosync/resell-tracker"), True)
        check("janitor: a sibling folder that only starts with the name is NOT a real app repo",
              _is_real_app_repo(_GH_PROJECTS_ROOT + " old/resell-tracker"), False)

        # dead-slice predicate (hermetic slice-runs dir)
        _saved_sr = _SLICE_RUNS_DIR
        _SLICE_RUNS_DIR = _td / "slice-runs"
        _SLICE_RUNS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            check("janitor: failed fragment + run-state GONE -> dead",
                  _is_dead_slice_fragment("bfmrAutoLink-s3-if-it-does", "failed"), True)
            (_SLICE_RUNS_DIR / "arr-codec-floor.json").write_text("{}")
            check("janitor: failed fragment + run-state PRESENT -> not dead",
                  _is_dead_slice_fragment("arr-codec-floor-s1-codec-rank", "failed"),
                  False)
            check("janitor: a DONE fragment is never dead (chain live)",
                  _is_dead_slice_fragment("bfmrAutoLink-s3-if", "done"), False)
            check("janitor: an auto-author-/auto-refine- fragment of a LIVE chain is "
                  "not dead (wrapper stripped before the run-state lookup)",
                  (_is_dead_slice_fragment(
                       "auto-author-arr-codec-floor-s1-codec-rank", "failed"),
                   _is_dead_slice_fragment(
                       "auto-refine-arr-codec-floor-s1-codec-rank-r2", "failed")),
                  (False, False))
            check("janitor: an auto-author- fragment whose chain is GONE is dead",
                  _is_dead_slice_fragment("auto-author-bfmrAutoLink-s3-if", "failed"),
                  True)
            check("janitor: a non-slice label is never a dead fragment",
                  _is_dead_slice_fragment("fix-order-919", "failed"), False)
        finally:
            _SLICE_RUNS_DIR = _saved_sr

        # STAGE-DEDUP: keep only the latest row; archive earlier author/refine stages
        check("janitor: author rank 0",
              _feature_and_rank("auto-author-foo"), ("foo", 0))
        check("janitor: refine rank N",
              _feature_and_rank("auto-refine-foo-r3"), ("foo", 3))
        check("janitor: bare feature (final row) rank +inf",
              _feature_and_rank("foo")[1] == float("inf"), True)
        _rows = [
            {"id": "auth", "label": "auto-author-arr-grab-top"},
            {"id": "ref1", "label": "auto-refine-arr-grab-top-r1"},
            {"id": "ref2", "label": "auto-refine-arr-grab-top-r2"},
            {"id": "final", "label": "arr-grab-top"},
            {"id": "elsewhere", "label": "auto-author-other-thing"},
            {"id": "owed", "label": "auto-refine-arr-grab-top-r3",
             "awaiting_signoff": True}]
        check("janitor stage-dedup: final/terminal row archives all earlier stages",
              {r["id"] for r in select_superseded_siblings(_rows, "arr-grab-top")},
              {"auth", "ref1", "ref2"})
        check("janitor stage-dedup: rN archives only STRICTLY-earlier rounds",
              {r["id"] for r in
               select_superseded_siblings(_rows, "auto-refine-arr-grab-top-r2")},
              {"auth", "ref1"})
        check("janitor stage-dedup: a sign-off-owed stage is NEVER archived",
              "owed" not in {r["id"] for r in
                             select_superseded_siblings(_rows, "arr-grab-top")}, True)

        # LIVE driver with injected fetch/post (no real HTTP): eval self archives +
        # a clean coding deliverable SURFACES but its superseded stages archive.
        _saved_jmode = RUNSTATUS_JANITOR_MODE
        RUNSTATUS_JANITOR_MODE = "live"
        try:
            _state([{"id": "evalarm1", "status": "done", "label": "clamp-arm7",
                     "cwd": "/tmp/bakeoff/wt"}])
            _fz = [{"id": "aauth", "label": "auto-author-featZ"},
                   {"id": "rref1", "label": "auto-refine-featZ-r1"}]
            _p1 = []
            _pj1 = {"verdict": "concerns", "job_label": "clamp-arm7",
                    "job_cwd": "/tmp/bakeoff/wt"}
            runstatus_janitor_consider(
                "evalarm1", _pj1, _td / "ollama-queue-logs" / "evalarm1.gate.json",
                fetch=lambda: _fz, post=lambda j: (_p1.append(j) or {"ok": True}))
            check("janitor live: eval-arm self is archived",
                  "evalarm1" in _p1, True)
            check("janitor live: self decision recorded auto-archive",
                  _pj1["runstatus_janitor"]["self"]["action"], "auto-archive")

            _state([{"id": "codez", "status": "done", "label": "featZ",
                     "cwd": _GH_PROJECTS_ROOT + "/resell-tracker"}])
            _p2 = []
            _pj2 = {"verdict": "pass", "job_label": "featZ",
                    "job_cwd": _GH_PROJECTS_ROOT + "/resell-tracker"}
            runstatus_janitor_consider(
                "codez", _pj2, _td / "ollama-queue-logs" / "codez.gate.json",
                fetch=lambda: _fz, post=lambda j: (_p2.append(j) or {"ok": True}))
            check("janitor live: clean coding deliverable self SURFACES (not cleared)",
                  "codez" in _p2, False)
            check("janitor live: its superseded author/refine stages ARE cleared",
                  set(_p2), {"aauth", "rref1"})
            # idempotent: a second pass over the same payload does nothing new
            _p3 = []
            runstatus_janitor_consider(
                "codez", _pj2, _td / "ollama-queue-logs" / "codez.gate.json",
                fetch=lambda: _fz, post=lambda j: (_p3.append(j) or {"ok": True}))
            check("janitor live: idempotent (marker) -> no re-archive", _p3, [])
        finally:
            RUNSTATUS_JANITOR_MODE = _saved_jmode

        # advisory invariant: the janitor never raises, even on garbage.
        _jr = False
        try:
            runstatus_janitor_consider("bad3", {"verdict": object()},
                                       _td / "ollama-queue-logs" / "bad3.gate.json",
                                       fetch=lambda: (_ for _ in ()).throw(ValueError),
                                       post=lambda j: {"ok": True})
        except Exception:
            _jr = True
        check("janitor: consider swallows all errors (never raises)", _jr, False)

        # --- RISK-TIERED auto-confirm (2026-09-17) -------------------------
        # The property: the machine never seals a model-authored fixture, without
        # a human reading it, on anything that touches money/P&L, DB writes or
        # destructive paths -- and never at all unless the automated relevance
        # pass ran clean. Anchored on rt-pl-exclude, where the human review the
        # DRAFT_UNCONFIRMED marker stands for caught a P&L bug the mutation gate
        # had passed.
        _lint_p = _orig_bin / "faithful-harness-lint.py"
        _lint = None
        if _lint_p.exists():
            _ls = importlib.util.spec_from_file_location("_fh_lint_st", _lint_p)
            _lint = importlib.util.module_from_spec(_ls)
            _ls.loader.exec_module(_lint)
        _FAITHFUL_FIX = _SELFTEST_FAITHFUL_FIXTURE
        _PROXY_FIX = (
            'CAPTURED = {"where": {}}\n'
            'def main():\n'
            '    for c in [CAPTURED]:\n'
            '        assert c.where == {}\n')

        def _mkwt(name, task, fixture):
            d = _td / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "TASK.md").write_text(task)
            (d / "test_fixture.py").write_text(fixture)
            return d

        _benign_task = ("Fix the codec rank ordering in ranker.py so a higher "
                        "tier never sorts below a lower one. Only edit ranker.py.")
        _low = _mkwt("wt_low", _benign_task, _FAITHFUL_FIX)
        check("risk-tier: benign + clean lint is LOW",
              _auto_confirm_risk(_low, {}, lint_mod=_lint)[0], "low")

        for _name, _task in (
                ("money", "Exclude unsubmitted gift cards from the P&L profit "
                          "calculation. Only edit route.ts."),
                ("dbwrite", "Use prisma.order.updateMany to backfill the flag. "
                            "Only edit sync.ts."),
                ("destructive", "Guard the DELETE FROM orders path so a partial "
                                "match cannot truncate the table.")):
            _wt = _mkwt(f"wt_{_name}", _task, _FAITHFUL_FIX)
            _t, _r = _auto_confirm_risk(_wt, {}, lint_mod=_lint)
            check(f"risk-tier: {_name} surface requires a human", _t, "human")
            check(f"risk-tier: {_name} names why", bool(_r), True)

        if _lint is not None:
            _proxy = _mkwt("wt_proxy", _benign_task, _PROXY_FIX)
            check("risk-tier: a proxy fixture requires a human (lint is "
                  "MANDATORY before any auto-seal)",
                  _auto_confirm_risk(_proxy, {}, lint_mod=_lint)[0], "human")

        # Fail closed on an unreadable / empty tree, never open.
        _empty = _td / "wt_empty"
        _empty.mkdir(parents=True, exist_ok=True)
        check("risk-tier: empty worktree fails CLOSED",
              _auto_confirm_risk(_empty, {}, lint_mod=_lint)[0], "human")
        check("risk-tier: missing worktree fails CLOSED",
              _auto_confirm_risk(_td / "nope", {}, lint_mod=_lint)[0], "human")
        # No fixture to certify is not a pass either.
        _nofix = _td / "wt_nofix"
        _nofix.mkdir(parents=True, exist_ok=True)
        (_nofix / "TASK.md").write_text(_benign_task)
        check("risk-tier: no fixture to lint requires a human",
              _auto_confirm_risk(_nofix, {}, lint_mod=_lint)[0], "human")

        # --- ESCALATION TRIAGE (Task E) ---------------------------------------
        # Golden case, taken from the REAL reviewer-code-high parks: the reviewer
        # flags a line the diff REMOVED, and TASK.md itself quotes that line as the
        # behaviour being changed. Every guard below is revert-tested: flip ONE
        # precondition and the decision must fall back to park.
        _t_task = (
            "# Task: token torrent match\n\n"
            "## Defect 2, CONFIRMED (arr-webhook.py `find_new_torrent_hash`, ~line 526)\n"
            "The match is `if torrent_name in new_name or new_name in torrent_name:` -- "
            "bidirectional substring. A short name like \"21\" substrings \"2021\".\n\n"
            "## The property to satisfy\n`find_new_torrent_hash` must identify the new "
            "torrent by whole-word token match, not substring.\n")
        _t_diff = (
            "--- a/arr-webhook.py\n+++ b/arr-webhook.py\n@@ -526,7 +526,7 @@\n"
            "     for h, torrent_name in torrents:\n"
            "-        if torrent_name in new_name or new_name in torrent_name:\n"
            "+        if torrent_matches_any_title(new_name, [torrent_name]):\n"
            "             return h\n")
        _t_find = [{"severity": "high",
                    "quote": "if torrent_name in new_name or new_name in torrent_name:"}]

        def _tpl(**over):
            p = {"auto_fix_class": "reviewer-code-high", "auto_fix_action": "escalate",
                 "verdict": "fail", "job_label": "plex-pathmatch",
                 "counts": {"code_high": 1, "code": 1, "input": 0, "total": 1},
                 "issues": [{"severity": "high", "file": "arr-webhook.py", "line": 531,
                             "what": "removed the substring match", "category": "code",
                             "source": "review"}],
                 "verify_exit_reported": 0, "verify_quality_exit": 0, "job_exit_code": 0,
                 "verify_failed_at_baseline": True, "not_checked": [],
                 "launch_baseline": {"head": "abc", "dirty": 0},
                 "verify_relevance": {"verdict": "relevant", "score": 1.0,
                                      "threshold": 0.8, "killed": 10, "survived": 0,
                                      "truncated": False}}
            p.update(over)
            return p

        def _dec(payload=None, findings=None, task=None, diff=None):
            return classify_escalation_triage(
                payload if payload is not None else _tpl(),
                _t_find if findings is None else findings,
                _t_task if task is None else task,
                _t_diff if diff is None else diff)

        check("triage: golden intended-removal auto-resolves",
              _dec()["action"], "auto-resolve")
        check("triage: golden class names the proof",
              _dec()["class"], "intended-removal-proven")
        check("triage: golden cites the contract",
              _dec()["citations"][0]["cites_contract"], True)
        # Not this class / not an escalation -> nothing to triage at all.
        check("triage: a different park class is SKIPPED",
              _dec(_tpl(auto_fix_class="ladder-exhausted"))["action"], "skip")
        check("triage: a requeue (not an escalation) is SKIPPED",
              _dec(_tpl(auto_fix_action="requeue"))["action"], "skip")
        # REVERT-TESTS: each guard must BITE on its own.
        for _name, _p in [
                ("verify red", _tpl(verify_exit_reported=1)),
                ("verify-quality red", _tpl(verify_quality_exit=1)),
                ("job exit nonzero", _tpl(job_exit_code=1)),
                ("verify never proven red at baseline",
                 _tpl(verify_failed_at_baseline=False)),
                ("relevance not 'relevant'",
                 _tpl(verify_relevance={"verdict": "weak", "score": 0.4, "threshold": 0.8,
                                        "killed": 4, "survived": 6})),
                ("a mutant SURVIVED",
                 _tpl(verify_relevance={"verdict": "relevant", "score": 1.0,
                                        "threshold": 0.8, "killed": 9, "survived": 1})),
                ("relevance truncated",
                 _tpl(verify_relevance={"verdict": "relevant", "score": 1.0,
                                        "threshold": 0.8, "killed": 10, "survived": 0,
                                        "truncated": True})),
                ("an input/scope finding exists",
                 _tpl(counts={"code_high": 1, "code": 1, "input": 1, "total": 2})),
                ("dirty launch baseline", _tpl(launch_baseline={"head": "a", "dirty": 3})),
                ("the gate left something unmeasured", _tpl(not_checked=["verify"])),
                ("more counted highs than quoted findings",
                 _tpl(counts={"code_high": 2, "code": 2, "input": 0, "total": 2})),
                ("flagged file is NOT in the TASK's scope",
                 _tpl(issues=[{"severity": "high", "file": "lib/unrelated.py",
                               "what": "x", "category": "code"}])),
        ]:
            check(f"triage: PARKS when {_name}", _dec(_p)["action"], "park")
        check("triage: parks with NO evidence (missing TASK)", _dec(task="")["action"], "park")
        check("triage: parks with NO diff", _dec(diff="")["action"], "park")
        check("triage: parks with no quoted finding", _dec(findings=[])["action"], "park")
        # The DISCRIMINATOR itself: a genuine regression flags ADDED code, and a
        # removal TASK.md never mentions is not provably contract-mandated.
        check("triage: PARKS when the flagged literal is ADDED (live) code, not removed",
              _dec(findings=[{"severity": "high",
                              "quote": "if torrent_matches_any_title(new_name, [torrent_name]):"}]
                   )["action"], "park")
        _readd = _t_diff + "+        if torrent_name in new_name or new_name in torrent_name:\n"
        check("triage: PARKS when the literal was removed AND re-added",
              _dec(diff=_readd)["action"], "park")
        _nocite = "# Task: speed up the matcher in arr-webhook.py\n\n## The property\nBe faster.\n"
        check("triage: PARKS when TASK.md never quotes the removed literal",
              _dec(task=_nocite)["action"], "park")
        check("triage: ...and says CITATION is what is missing (not scope)",
              "does not quote the removed literal" in " ".join(_dec(task=_nocite)["reasons"]),
              True)
        check("triage: a file-header '--- a/x' line is not a REMOVED line",
              _triage_diff_lines("--- a/x.py\n+++ b/x.py\n-real_removed()\n")[0],
              ["real_removed()"])
        check("triage: citation run needs LENGTH, a short overlap is not a cite",
              classify_escalation_triage(_tpl(), _t_find, _t_task, _t_diff,
                                         min_cite=400)["action"], "park")
        check("triage: a normalised quote still matches across reflow/trailing comma",
              _longest_common_run(_triage_norm("shippingAddress: existing.shippingAddress "
                                               "||  (r.shippingAddress || null),"),
                                  _triage_norm("line 330 'shippingAddress: "
                                               "existing.shippingAddress || (r.shippingAddress "
                                               "|| null)' unconditionally freezes it")), 72)

        # --- STAGE 4: CONTRACT SUPERSESSION -----------------------------------
        # Golden case, reduced from the REAL park bg-eraser-s1-invoke-s4-profile-
        # full-name (0eb122a99cb9): TASK.md mandates a whole-signature rewrite, the
        # model obeys, and the harness's literal-set guard then flags the OLD default
        # and the OLD --flag as "silently dropped". Stage 3 cannot clear these --
        # TASK.md quotes the REPLACEMENT, never the removed line.
        _s4_task = (
            "# TASK: eraser-name\n\n"
            "## Confirmed defect (observed, not suspected)\n"
            "`build_eraser_cmd` in `broker_guard/eraser.py` builds the command from a\n"
            "profile *file path* -- it never passes the person's name to the engine.\n\n"
            "## Required change\n\n"
            "Change `build_eraser_cmd` in `broker_guard/eraser.py` so it takes the loaded\n"
            "identity profile as a dict. Exact contract (asserted byte-for-byte):\n\n"
            "- Signature, kept exactly: `def build_eraser_cmd(broker_id: str, profile: "
            "dict, eraser_bin: str = 'eraser') -> list[str]:`\n"
            "- Otherwise return exactly: `[eraser_bin, \"remove\", \"--name\", full_name, "
            "\"--broker\", normalized]`\n\n"
            "Behaviour that must NOT change:\n"
            "- non-string `broker_id` still raises `ValueError`\n"
            "- `eraser_bin` remains the first argv element\n\n"
            "## Must contain\n\n"
            "- `def build_eraser_cmd(broker_id: str, profile: dict, eraser_bin: str = "
            "'eraser') -> list[str]:`\n")
        _s4_old_def = ("def build_eraser_cmd(broker_id: str, profile_path: str = "
                       "\"profile.local.json\", eraser_bin: str = 'eraser') -> list[str]:")
        _s4_new_def = ("def build_eraser_cmd(broker_id: str, profile: dict, "
                       "eraser_bin: str = 'eraser') -> list[str]:")
        _s4_old_ret = ("return [eraser_bin, \"remove\", \"--profile\", profile_path, "
                       "\"--broker\", normalized]")
        _s4_diff = (
            "diff --git a/broker_guard/eraser.py b/broker_guard/eraser.py\n"
            "--- a/broker_guard/eraser.py\n+++ b/broker_guard/eraser.py\n"
            "@@ -4,10 +4,13 @@ import re\n"
            f"-{_s4_old_def}\n"
            f"+{_s4_new_def}\n"
            "     normalized = broker_id.strip().lower()\n"
            f"-    {_s4_old_ret}\n"
            "+    full_name = profile['full_name']\n"
            "+    return [eraser_bin, \"remove\", \"--name\", full_name, \"--broker\", "
            "normalized]\n")
        _s4_find = [{"severity": "high", "source": "harness-dropped-member",
                     "quote": _s4_old_def},
                    {"severity": "high", "source": "harness-dropped-member",
                     "quote": _s4_old_ret}]

        def _s4pl(**over):
            _i = {"severity": "high", "file": "broker_guard/eraser.py",
                  "what": "silently dropped", "category": "code", "source": "review"}
            p = _tpl(counts={"code_high": 2, "code": 2, "input": 0, "total": 2},
                     issues=[dict(_i, line=7), dict(_i, line=13)])
            p.update(over)
            return p

        def _s4(payload=None, findings=None, task=None, diff=None):
            return classify_escalation_triage(
                payload if payload is not None else _s4pl(),
                _s4_find if findings is None else findings,
                _s4_task if task is None else task,
                _s4_diff if diff is None else diff)

        check("stage4: golden contract-supersession is DETECTED",
              _s4()["class"], "contract-supersession-shadow")
        check("stage4: SHADOW BY CONSTRUCTION -- a match still PARKS, never resolves",
              _s4()["action"], "park")
        check("stage4: both flagged removals are marked superseded",
              [c["superseded"] for c in _s4()["citations"]], [True, True])
        check("stage4: stage 3 alone could NOT have cleared it (no verbatim cite)",
              [c["cites_contract"] for c in _s4()["citations"]][1], False)
        check("stage4: the reason NAMES the superseding signature",
              any(_s4_new_def[:40] in r for r in _s4()["reasons"]), True)
        check("stage4: the reason says out loud that it never auto-resolves",
              "SHADOW-ONLY" in _s4()["reasons"][0], True)
        check("stage4: names the rewritten function and the dropped tokens",
              (_s4()["supersessions"][1]["func"],
               _s4()["supersessions"][1]["dropped"]),
              ("build_eraser_cmd", ["--profile", "profile_path"]))

        # NEGATIVE CONTROL 1 -- COLLATERAL DAMAGE. Same contract-mandated rewrite, but
        # TASK.md's must-NOT-change list explicitly PRESERVES what this line dropped.
        # That is a real regression wearing a rewrite's clothes: it must still park.
        _s4_pres = _s4_task.replace(
            "- `eraser_bin` remains the first argv element",
            "- `eraser_bin` remains the first argv element\n"
            "- the `--profile` flag and `profile_path` are still passed to the engine")
        check("stage4 NEG: a drop the contract PRESERVES is NOT supersession",
              [s["ok"] for s in _s4(task=_s4_pres)["supersessions"]], [False, False])
        check("stage4 NEG: ...and the job parks as an unproven removal",
              _s4(task=_s4_pres)["class"], "not-a-proven-intended-removal")
        check("stage4 NEG: ...naming the contract term it would have violated",
              _s4(task=_s4_pres)["supersessions"][1]["preserved_by_contract"],
              ["--profile", "profile_path"])

        # NEGATIVE CONTROL 2 -- the contract only PARAPHRASES the new signature. The
        # whole-line exact citation is the load-bearing evidence; without it, nothing.
        _s4_nosig = _s4_task.replace(_s4_new_def, "it should take the profile dict now")
        check("stage4 NEG: a paraphrased signature is not a dictated one",
              _s4(task=_s4_nosig)["supersessions"], [])
        check("stage4 NEG: ...so the job parks",
              _s4(task=_s4_nosig)["class"], "not-a-proven-intended-removal")

        # NEGATIVE CONTROL 3 -- the removal is in a DIFFERENT function, past a hunk
        # boundary. A rewrite of build_eraser_cmd licenses nothing over there.
        _s4_far = _s4_diff + ("@@ -40,6 +40,5 @@\n"
                              "     def submit(payload):\n"
                              "-        audit_log.write(payload)\n"
                              "         return post(payload)\n")
        check("stage4 NEG: a removal in another function is NOT superseded",
              _s4(diff=_s4_far, findings=[{"severity": "high",
                                           "quote": "audit_log.write(payload)"}],
                  payload=_s4pl(counts={"code_high": 1, "code": 1, "input": 0,
                                        "total": 1}))["supersessions"], [])

        # NEGATIVE CONTROL 4 -- the signature never changed, only the body. Nothing
        # was "redesigned", so a dropped body line is just a dropped body line.
        _s4_same = _s4_diff.replace(f"-{_s4_old_def}\n+{_s4_new_def}\n", f" {_s4_new_def}\n")
        check("stage4 NEG: an UNCHANGED signature licenses no body removal",
              _s4(diff=_s4_same, findings=[_s4_find[1]],
                  payload=_s4pl(counts={"code_high": 1, "code": 1, "input": 0,
                                        "total": 1}))["supersessions"], [])

        # NEGATIVE CONTROL 5 -- flagged code is ADDED, not removed (the real shape of
        # the second finding in park 813d71882974). Never supersession, ever.
        check("stage4 NEG: ADDED code is never superseded",
              _s4(findings=[{"severity": "high",
                             "quote": "full_name = profile['full_name']"}],
                  payload=_s4pl(counts={"code_high": 1, "code": 1, "input": 0,
                                        "total": 1}))["supersessions"], [])

        # MIXED, from real park 813d71882974: one genuine high on ADDED code plus one
        # superseded MEDIUM. The job must still park on the genuine finding -- while
        # the log still EXPLAINS the medium, which is the whole point of stage 4.
        _s4_mixed = _s4(findings=[{"severity": "high",
                                   "quote": "full_name = profile['full_name']"},
                                  dict(_s4_find[1], severity="medium")],
                        payload=_s4pl(counts={"code_high": 1, "code": 2, "input": 0,
                                              "total": 2}))
        check("stage4 MIXED: a genuine added-code high still parks the job",
              _s4_mixed["class"], "not-a-proven-intended-removal")
        check("stage4 MIXED: the superseded MEDIUM is still explained in the log",
              [(s["ok"], s["severity"]) for s in _s4_mixed["supersessions"]],
              [(True, "medium")])

        # The rewrite scanner itself.
        check("stage4: scanner pairs -def/+def of the same function",
              [(r["name"], bool(r["old"]), bool(r["new"]))
               for r in _triage_signature_rewrites(_s4_diff)],
              [("build_eraser_cmd", True, True)])
        check("stage4: scanner never attributes a body line across a hunk gap",
              _triage_signature_rewrites(_s4_far)[0]["removed"], [_s4_old_ret])
        check("stage4: must-NOT-change block stops at the next heading",
              "must contain" in _triage_must_not_change(_s4_task), False)
        check("stage4: must-NOT-change block captures its bullets",
              "remains the first argv element"
              in _triage_must_not_change(_s4_task), True)
        check("stage4: PRESERVE lookup sees a token inside markdown backticks",
              "eraser_bin" in _triage_must_not_change(_s4_task), True)

        # EFFECTOR: shadow must resolve NOTHING; live resolves exactly once.
        _saved_tmode = ESCALATION_TRIAGE_MODE
        _tlogs = _td / "ollama-queue-logs"
        try:
            _calls = []

            class _R:
                returncode = 0
                stdout = stderr = ""

            def _fakerun(argv):
                _calls.append(argv)
                return _R()

            def _mk(jid):
                _p = _tpl(auto_fix_escalate_status="parked-needs_opus")
                _g = _tlogs / f"{jid}.gate.json"
                (_tlogs / f"{jid}.diff").write_text(_t_diff)
                _rd = _tlogs / f"{jid}-review"
                _rd.mkdir(parents=True, exist_ok=True)
                (_rd / "run.json").write_text(json.dumps({"findings": _t_find}))
                (_rd / "task.json").write_text(json.dumps({"intent": _t_task}))
                _p["review_dir"] = str(_rd)
                _g.write_text(json.dumps(_p))
                return _p, _g

            ESCALATION_TRIAGE_MODE = "shadow"
            _ps, _gs = _mk("trg_s")
            escalation_triage_consider("trg_s", _ps, _gs, run=_fakerun)
            check("triage effector: shadow DECIDES auto-resolve",
                  _ps["escalation_triage"]["action"], "auto-resolve")
            check("triage effector: shadow resolves NOTHING", _calls, [])
            check("triage effector: shadow still records the exact resolve command",
                  _ps["escalation_triage_cmd"][2:], ["resolve", "trg_s"])
            check("triage effector: the decision is on disk for the drain",
                  json.loads(_gs.read_text())["escalation_triage"]["class"],
                  "intended-removal-proven")
            check("triage effector: an auto-resolve is never silent (audit row)",
                  "trg_s" in (_tlogs / "ESCALATION-TRIAGE.md").read_text(), True)

            ESCALATION_TRIAGE_MODE = "live"
            _pl, _gl = _mk("trg_l")
            escalation_triage_consider("trg_l", _pl, _gl, run=_fakerun)
            check("triage effector: live RESOLVES the park",
                  [c[2:] for c in _calls], [["resolve", "trg_l"]])
            check("triage effector: live records the outcome",
                  _pl.get("escalation_triage_status"), "resolved-false-positive")
            escalation_triage_consider("trg_l", _pl, _gl, run=_fakerun)
            check("triage effector: idempotent -- never resolves twice", len(_calls), 1)

            # A park decision must NEVER resolve, in live mode either.
            _pp, _gp = _mk("trg_p")
            _pp["verify_failed_at_baseline"] = False
            escalation_triage_consider("trg_p", _pp, _gp, run=_fakerun)
            check("triage effector: live NEVER resolves a park decision", len(_calls), 1)
            check("triage effector: a park still attaches its analysis",
                  _pp["escalation_triage"]["class"], "prerequisites-unmet")
            # Never clears a park that never actually happened.
            _pn, _gn = _mk("trg_n")
            _pn.pop("auto_fix_escalate_status", None)
            escalation_triage_consider("trg_n", _pn, _gn, run=_fakerun)
            check("triage effector: never resolves a job that never parked", len(_calls), 1)

            # STAGE 4 SAFETY VALVE, the test that matters most: a FULL contract-
            # supersession match, in LIVE mode, on a genuinely parked job, must STILL
            # resolve nothing -- and must still write its analysis to the audit log.
            _p4 = _s4pl(auto_fix_escalate_status="parked-needs_opus")
            _g4 = _tlogs / "trg_s4.gate.json"
            (_tlogs / "trg_s4.diff").write_text(_s4_diff)
            _rd4 = _tlogs / "trg_s4-review"
            _rd4.mkdir(parents=True, exist_ok=True)
            (_rd4 / "run.json").write_text(json.dumps({"findings": _s4_find}))
            (_rd4 / "task.json").write_text(json.dumps({"intent": _s4_task}))
            _p4["review_dir"] = str(_rd4)
            _g4.write_text(json.dumps(_p4))
            escalation_triage_consider("trg_s4", _p4, _g4, run=_fakerun)
            check("stage4 effector: LIVE mode resolves NOTHING on a supersession",
                  len(_calls), 1)
            check("stage4 effector: ...and records no resolve command at all",
                  _p4.get("escalation_triage_cmd"), None)
            check("stage4 effector: ...but DOES attach the explanation",
                  _p4["escalation_triage"]["class"], "contract-supersession-shadow")
            check("stage4 effector: ...and writes a SUPERSEDED audit row",
                  "SUPERSEDED" in (_tlogs / "ESCALATION-TRIAGE.md").read_text(), True)
        finally:
            ESCALATION_TRIAGE_MODE = _saved_tmode

        # --- 2026-09-24: a gate must never end in SILENCE (arr-codec-floor s4 r3) ---
        _txt, _note = _read_review_intent(str(_td / "nope" / "AUTO-TASK.md"))
        check("review intent: a vanished task file degrades to a note, never raises",
              (bool(_txt), "vanished" in _note, "FileNotFoundError" in _note), (True, True, True))
        _tf_ok = _td / "TASK-ok.md"
        _tf_ok.write_text("# TASK\nfix it\n")
        check("review intent: a readable task file is returned whole with no note",
              _read_review_intent(str(_tf_ok)), ("# TASK\nfix it\n", ""))
        _cod = _td / "crash-out"
        try:
            raise FileNotFoundError(2, "No such file or directory", "/x/AUTO-TASK.md")
        except FileNotFoundError as _e:
            _exc = _e
        _cargv = ["--job-id", "crash1", "--job-label", "auto-refine-x-r3",
                  "--cwd", "/x", "--task-file", "/x/AUTO-TASK.md", "--verify", "python3 v.py"]
        _gj = crash_verdict(_cargv, _exc, out_dir=_cod)
        _rec1 = json.loads(_gj.read_text()) if _gj else {}
        check("crash guard: writes a TERMINAL `error` verdict sidecar for the job",
              _rec1.get("verdict"), "error")
        check("crash guard: the record names the exception, where it happened, and the job",
              ("FileNotFoundError" in _rec1.get("reason", ""), "in _self_test" in _rec1.get("reason", ""),
               _rec1.get("job_id"), _rec1.get("job_label"), _rec1.get("job_verify")),
              (True, True, "crash1", "auto-refine-x-r3", "python3 v.py"))
        check("crash guard: `error` is a slicer REDISPATCH verdict (the chain re-enters its bounded retry)",
              "error" in SLICE_REDISPATCH_VERDICTS, True)
        check("crash guard: never clobbers a verdict main() already emitted",
              crash_verdict(_cargv, _exc, out_dir=_cod), None)
        check("crash guard: a gate-/regate- MERGE invocation writes nothing (the id is the review job's)",
              (crash_verdict(["--job-id", "rev9", "--job-label", "gate-crash1"], _exc, out_dir=_cod),
               crash_verdict(["--job-id", "rev9", "--job-label", "regate-crash1"], _exc, out_dir=_cod),
               (_cod / "rev9.gate.json").exists()), (None, None, False))
        check("crash guard: no --job-id -> nothing to name, writes nothing",
              crash_verdict(["--cwd", "/x"], _exc, out_dir=_cod), None)
        # END TO END: main() raising the live exception under the guard -> exit 0 + a
        # verdict on disk. Revert test first: the unguarded entry has no such record.
        _saved_main = globals()["main"]
        _saved_argv = sys.argv
        def _boom():
            raise FileNotFoundError(2, "No such file or directory",
                                    str(_cod / "wt" / "AUTO-TASK.md"))
        globals()["main"] = _boom
        try:
            sys.argv = ["gate-on-complete.py", "--job-id", "crash2", "--job-label",
                        "auto-refine-y-r1", "--cwd", str(_cod / "wt"),
                        "--out-dir", str(_cod)]
            _rc = _guarded_main(consider=False)
        finally:
            globals()["main"] = _saved_main
            sys.argv = _saved_argv
        check("crash guard e2e: a crashing main() exits 0 under the guard (rule 2)", _rc, 0)
        check("crash guard e2e: ...and left a terminal error verdict for the job",
              json.loads((_cod / "crash2.gate.json").read_text()).get("verdict"), "error")
        check("crash guard e2e: a non-crashing main() passes its exit code through",
              (lambda: (globals().__setitem__("main", lambda: 7),
                        _guarded_main(argv=["--job-id", "ok1"], consider=False),
                        globals().__setitem__("main", _saved_main))[1])(), 7)

        # ---- CROSS-FAMILY SECOND OPINION (Unraid, 2026-10-01) ----------------
        # The whole feature's value rests on two properties, so both are tested
        # DIRECTLY and not via a proxy: (a) it only fires cross-family, (b) it can
        # never change a verdict.
        check("second opinion: _model_family separates the gate tiers from the pick",
              (_model_family("qwen3:14b"), _model_family("qwen3.6-35b-a3b-vl-mtp-mxfp8"),
               _model_family("gemma4:12b-it-q4_K_M"), _model_family("phi4:14b"),
               _model_family("mistral:latest"),
               _model_family("hf.co/unsloth/Seed-Coder-8B-Instruct-GGUF:Q4_K_M")),
              ("qwen", "qwen", "gemma", "phi", "mistral", "seed"))
        check("second opinion: the shipped default IS a different family from both gate tiers",
              (_model_family(SECOND_OPINION_MODEL) not in
               {_model_family(PREGATE_MODEL), _model_family(REGATE_MODEL)},
               _model_family(SECOND_OPINION_MODEL)), (True, "gemma"))

        _sod = _td / "secondop-out"
        _sod.mkdir(parents=True, exist_ok=True)
        (_sod / "p1.diff").write_text(
            "--- a/app/x.py\n+++ b/app/x.py\n@@ -1 +1,2 @@\n-old\n+new\n+more\n")
        (_sod / "p2.diff").write_text(
            "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-a\n+b\n")
        check("second opinion: a PASSing code diff is eligible",
              _second_opinion_eligible({"verdict": "pass"}, _sod / "p1.diff")[0], True)
        check("second opinion: a non-PASS verdict needs no corroboration",
              _second_opinion_eligible({"verdict": "fail"}, _sod / "p1.diff")[0], False)
        check("second opinion: a docs-only diff is not a code diff",
              _second_opinion_eligible({"verdict": "pass"}, _sod / "p2.diff")[0], False)
        check("second opinion: never requested twice for the same parent",
              _second_opinion_eligible({"verdict": "pass", "second_opinion_label": "secondop-p1"},
                                       _sod / "p1.diff")[0], False)
        check("second opinion: a missing diff is not invented work",
              _second_opinion_eligible({"verdict": "pass"}, _sod / "nope.diff")[0], False)
        # SAME-FAMILY REFUSAL -- the guard that makes this a second OPINION and not a
        # second qwen. Revert-style: flip the model to a qwen and it must refuse.
        _so_saved = globals()["SECOND_OPINION_MODEL"]
        try:
            globals()["SECOND_OPINION_MODEL"] = "qwen2.5-coder:14b"
            _f, _w = _second_opinion_eligible({"verdict": "pass"}, _sod / "p1.diff")
            check("second opinion: REFUSES a same-family (qwen) model -- not a second opinion",
                  (_f, _w.startswith("REFUSED"), "qwen" in _w), (False, True, True))
            _rp = {"verdict": "pass"}
            _rg = _sod / "p1.gate.json"
            _rg.write_text(json.dumps(_rp))
            request_second_opinion("p1", _rg, _rp, _sod)
            check("second opinion: a refused pick records WHY and requests nothing",
                  (_rp.get("second_opinion", {}).get("review"),
                   "second_opinion_label" in _rp), ("not-run", False))
        finally:
            globals()["SECOND_OPINION_MODEL"] = _so_saved
        # DISABLED by flag.
        _en_saved = globals()["SECOND_OPINION"]
        try:
            globals()["SECOND_OPINION"] = False
            check("second opinion: GATE_SECOND_OPINION=0 disables it",
                  _second_opinion_eligible({"verdict": "pass"}, _sod / "p1.diff"),
                  (False, "disabled (GATE_SECOND_OPINION=0)"))
        finally:
            globals()["SECOND_OPINION"] = _en_saved
        # REQUEST (TEST_MODE: records the intent, enqueues nothing).
        _tm_saved = globals()["TEST_MODE"]
        try:
            globals()["TEST_MODE"] = True
            _p = {"verdict": "pass", "issues": [], "counts": {"code_high": 0}}
            _g = _sod / "p1.gate.json"
            _g.write_text(json.dumps(_p))
            request_second_opinion("p1", _g, _p, _sod)
            _rec = json.loads(_g.read_text())
            check("second opinion: request records label/model/host and is ADVISORY-only",
                  (_rec.get("second_opinion_label"),
                   _rec["second_opinion"]["host"], _rec["second_opinion"]["num_ctx"],
                   "never flip" in _rec["second_opinion"]["authority"],
                   _rec["second_opinion"]["review"]),
                  ("secondop-p1", "unraid", SECOND_OPINION_NUM_CTX, True,
                   "skipped (GATE_TEST_MODE)"))
            check("second opinion: requesting it does NOT touch the verdict",
                  (_rec.get("verdict"), _rec.get("issues"), _rec.get("counts")),
                  ("pass", [], {"code_high": 0}))
            check("second opinion: the review task is cross-family and independent",
                  (json.loads((_sod / "p1-secondop" / "task.json").read_text())["mode"],
                   "INDEPENDENT SECOND OPINION" in
                   json.loads((_sod / "p1-secondop" / "task.json").read_text())["context"]),
                  ("review", True))
        finally:
            globals()["TEST_MODE"] = _tm_saved

        class _SoArgs:
            def __init__(self, jid, label, cwd):
                self.job_id, self.job_label, self.cwd = jid, label, cwd

        # AGREEING second opinion -> flagged as agreement, verdict untouched.
        _wt = _sod / "so-agree"; _wt.mkdir(parents=True, exist_ok=True)
        (_wt / "report.md").write_text("## VERDICT: PASS\n\nNothing introduced.\n")
        _g.write_text(json.dumps({"verdict": "pass", "issues": [],
                                  "counts": {"code_high": 0},
                                  "second_opinion_label": "secondop-p1",
                                  "second_opinion": {"review": "pending",
                                                     "model": "gemma4:12b-it-q4_K_M",
                                                     "family": "gemma"}}))
        merge_second_opinion(_SoArgs("so1", "secondop-p1", str(_wt)), _sod)
        _rec = json.loads(_g.read_text())
        check("second opinion: a PASS second opinion records agreement, verdict unchanged",
              (_rec["second_opinion_agreement"], _rec["second_opinion"]["review"],
               _rec["verdict"], _rec["issues"], "second_opinion_disagreement" in _rec),
              ("agree", "done", "pass", [], False))
        # DISAGREEING second opinion -> flags only. THIS is the property that matters.
        _wt2 = _sod / "so-dis"; _wt2.mkdir(parents=True, exist_ok=True)
        (_wt2 / "report.md").write_text(
            "## VERDICT: FAIL\n\n| # | sev | where | what |\n"
            "| 1 | high | `app/x.py:2` | new path returns before the write |\n")
        _g.write_text(json.dumps({"verdict": "pass", "issues": [],
                                  "counts": {"code_high": 0}, "gate_authority": "studio-27b-regate",
                                  "second_opinion_label": "secondop-p1",
                                  "second_opinion": {"review": "pending",
                                                     "model": "gemma4:12b-it-q4_K_M",
                                                     "family": "gemma"}}))
        merge_second_opinion(_SoArgs("so2", "secondop-p1", str(_wt2)), _sod)
        _rec = json.loads(_g.read_text())
        check("second opinion: DISAGREEMENT surfaces as a flag and NEVER flips the verdict",
              (_rec["verdict"], _rec["counts"], _rec["issues"],
               _rec["gate_authority"], _rec["second_opinion_agreement"],
               _rec["second_opinion_disagreement"],
               len(_rec["second_opinion_issues"]),
               "CROSS-FAMILY DISAGREEMENT" in _rec["second_opinion_flags"][0]),
              ("pass", {"code_high": 0}, [], "studio-27b-regate", "disagree",
               True, 1, True))
        check("second opinion: its findings stay OUT of payload['issues'] (no recompute can see them)",
              _rec["second_opinion_issues"][0]["source"], "second-opinion")
        # A finding citing a file not in the diff is dropped, same rule as the pre-gate.
        _wt3 = _sod / "so-uncorr"; _wt3.mkdir(parents=True, exist_ok=True)
        (_wt3 / "report.md").write_text(
            "## VERDICT: FAIL\n\n| # | sev | where | what |\n"
            "| 1 | high | `other/never_touched.py:9` | invented |\n")
        _g.write_text(json.dumps({"verdict": "pass", "issues": [], "counts": {},
                                  "second_opinion": {"review": "pending",
                                                     "model": "m", "family": "gemma"}}))
        merge_second_opinion(_SoArgs("so3", "secondop-p1", str(_wt3)), _sod)
        _rec = json.loads(_g.read_text())
        check("second opinion: a finding citing a file NOT in the diff is dropped as uncorroborated",
              (_rec["second_opinion_issues"],
               len(_rec["second_opinion"]["dropped_uncorroborated"])), ([], 1))
        # NO REPORT -> 'unknown', not a concern about the code.
        _wt4 = _sod / "so-noreport"; _wt4.mkdir(parents=True, exist_ok=True)
        _g.write_text(json.dumps({"verdict": "pass", "issues": [], "counts": {},
                                  "second_opinion": {"review": "pending", "model": "m"}}))
        merge_second_opinion(_SoArgs("so4", "secondop-p1", str(_wt4)), _sod)
        _rec = json.loads(_g.read_text())
        check("second opinion: a failed review is a MISSING MEASUREMENT, not a code concern",
              (_rec["verdict"], _rec["second_opinion"]["review"].startswith("failed"),
               _rec["second_opinion_agreement"].startswith("unknown")),
              ("pass", True, True))
        # PRIMARY REVIEW WITH NO REPORT (2026-10-06): 25 author records sat at
        # pass-pending-review forever. A pre-gate miss escalates to the authoritative
        # re-gate; an authoritative miss (or a cleared parent) is terminal concerns.
        _tm_saved = globals()["TEST_MODE"]
        try:
            globals()["TEST_MODE"] = True        # _escalate_regate must not enqueue
            _wt5 = _sod / "rv-noreport"; _wt5.mkdir(parents=True, exist_ok=True)
            _gp = _sod / "pnr.gate.json"
            _gp.write_text(json.dumps({"verdict": "pass-pending-review", "issues": [],
                                       "counts": {"code_high": 0, "code": 0, "input": 0,
                                                  "total": 0}}))
            merge_review(_SoArgs("rv1", "gate-pnr", str(_wt5)), _sod)
            _rec = json.loads(_gp.read_text())
            check("no-report pre-gate: pass-pending-review is ESCALATED to the "
                  "authoritative re-gate, never left dangling",
                  (_rec["review"].startswith("failed"), _rec.get("review_missing"),
                   _rec.get("regate"), _rec.get("regate_label")),
                  (True, "pregate", "skipped (GATE_TEST_MODE)", "regate-pnr"))
            _gp.write_text(json.dumps({"verdict": "pass-pending-review", "issues": [],
                                       "counts": {"code_high": 0}, "regate": "pending"}))
            merge_review(_SoArgs("rv2", "regate-pnr", str(_wt5)), _sod,
                         prefix="regate-", authoritative=True)
            _rec = json.loads(_gp.read_text())
            check("no-report authoritative: TERMINAL concerns naming the missing review",
                  (_rec["verdict"], _rec.get("gate_authority"),
                   [i["source"] for i in _rec["issues"]], _rec["counts"]["code_high"]),
                  ("concerns", "review-missing-terminal", ["review-missing"], 0))
            _gp.write_text(json.dumps({"verdict": "fail", "issues": [],
                                       "counts": {"code_high": 1}}))
            merge_review(_SoArgs("rv3", "gate-pnr", str(_wt5)), _sod)
            check("no-report: a verdict that is NOT pass-pending-review is left untouched",
                  json.loads(_gp.read_text())["verdict"], "fail")
        finally:
            globals()["TEST_MODE"] = _tm_saved
        # The merge only ever runs if the QUEUE re-invokes this hook for a
        # 'secondop-' completion. That lives in ollama-queue.py, so assert the wiring
        # rather than trust it: without this line the feature is silently dead.
        _qsrc = (Path.home() / "bin" / "ollama-queue.py")
        check("second opinion: ollama-queue re-invokes the hook for a 'secondop-' job",
              bool(_qsrc.is_file() and re.search(
                  r'_is_gate_job = _lbl\.startswith\(\([^)]*"secondop-"',
                  _qsrc.read_text(errors="replace"))), True)
        # ...and is NOT part of the gate BARRIER machinery (an advisory job must
        # never hold fresh authoring work behind it).
        check("second opinion: 'secondop-' is NOT in the queue's gate-barrier predicate",
              bool(_qsrc.is_file() and re.search(
                  r'def _is_gate_job\(job\):.*?startswith\(\("gate-", "regate-"\)\)',
                  _qsrc.read_text(errors="replace"), re.S)), True)
    finally:
        QUEUE_STATE, BIN, STATE_DIR, COMPLETED_ROOT = _orig_qs, _orig_bin, _orig_sd, _orig_cr
        # restore the model-ladder env the ladder tests overrode (fail-open).
        if _oldenv is None:
            os.environ.pop("OLLAMA_DISPATCH_MODEL_LADDER", None)
        else:
            os.environ["OLLAMA_DISPATCH_MODEL_LADDER"] = _oldenv
        _sh.rmtree(_td, ignore_errors=True)

    print("SELF_TEST_OK" if ok else "SELF_TEST_FAILED")
    return ok


def _argv_opt(argv, name, default=None):
    """`--name value` or `--name=value` from a raw argv list; never raises."""
    for i, tok in enumerate(argv):
        if tok == name and i + 1 < len(argv):
            return argv[i + 1]
        if tok.startswith(name + "="):
            return tok[len(name) + 1:]
    return default


def crash_verdict(argv, exc, out_dir=None):
    """RULE 2's LAST LINE (2026-09-24): main() raised after the queue fired this
    gate. Without this the process dies with a traceback in the daemon log and NO
    `<id>.gate.json` -- and every consumer of the verdict (the slicer's autofeed,
    signoff, the janitor, run-status, the queue's bundle focus, a human reading
    `status`) waits on silence. Live: 7511527da0d8 (arr-codec-floor s4 r3) ->
    FileNotFoundError on a reaped worktree's AUTO-TASK.md -> no verdict -> the slice
    sat FAILED with no event to re-enter it and the queue held focus on the dead
    bundle for 10 minutes. The same traceback sits at two earlier points in the
    daemon log; each was a silently lost verdict.

    Writes a minimal TERMINAL `error` verdict -- this file's existing convention for
    a gate that could not run (see the GATE-subprocess failure branch in main) -- so
    the pipeline has a record to act on: `error` is in SLICE_REDISPATCH_VERDICTS, so
    the slicer re-enters its bounded auto-retry. Never clobbers a record main()
    already emitted; never writes for a gate-/regate- MERGE invocation (that job id
    is the review job's, and the parent's record is the one that matters). Returns
    the path written, or None. Never raises (the caller is the crash handler)."""
    try:
        import traceback as _tb
        label = str(_argv_opt(argv, "--job-label", "") or "")
        job_id = str(_argv_opt(argv, "--job-id", "") or "")
        if not job_id or label.startswith(("gate-", "regate-", "secondop-")):
            return None
        _od_arg = _argv_opt(argv, "--out-dir")
        od = (Path(out_dir) if out_dir else
              Path(str(_od_arg)).expanduser() if _od_arg else STATE_DIR / "ollama-queue-logs")
        gj = od / f"{job_id}.gate.json"
        if gj.exists():
            return None
        frames = _tb.extract_tb(exc.__traceback__) if exc.__traceback__ else []
        where = (f"{Path(frames[-1].filename).name}:{frames[-1].lineno} in {frames[-1].name}"
                 if frames else "?")
        payload = {
            "verdict": "error",
            "reason": (f"gate crashed before emitting a verdict: {type(exc).__name__}: "
                       f"{str(exc)[:200]} (at {where})"),
            "job_id": job_id,
            "job_label": label or None,
            "job_cwd": str(_argv_opt(argv, "--cwd") or ""),
            "job_verify": _argv_opt(argv, "--verify") or None,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "advisory": True,
            "crash": {"type": type(exc).__name__, "message": str(exc)[:500], "where": where},
            "not_checked": ["everything past the crash point -- this record exists so the "
                            "verdict is not SILENCE; the traceback is in the daemon log"],
        }
        for k, v in (job_facts(job_id) or {}).items():
            payload.setdefault(k, v)
        od.mkdir(parents=True, exist_ok=True)
        gj.write_text(json.dumps(payload, indent=1))
        return gj
    except Exception:
        return None


def _guarded_main(argv=None, consider=True) -> int:
    """main() under the crash guard. A crash prints its traceback (the daemon log
    keeps the diagnostic), writes the terminal error verdict, hands it to the
    slicer's autofeed so the owning chain can re-enter its retry, and exits 0
    (rule 2: the gate never fails the dispatch). `consider=False` for --self-test."""
    argv = sys.argv[1:] if argv is None else argv
    try:
        return main()
    except SystemExit:
        raise
    except Exception as _crash:
        import traceback as _tb
        _tb.print_exc()
        gj = crash_verdict(argv, _crash)
        jid = _argv_opt(argv, "--job-id", "?")
        if gj is None:
            print(f"[gate] {jid} CRASHED ({type(_crash).__name__}) -- no verdict written "
                  f"(merge invocation, no job id, or a verdict already on disk)")
            return 0
        print(f"[gate] {jid} CRASHED ({type(_crash).__name__}) -> wrote terminal error "
              f"verdict {gj} so the pipeline sees a result, not silence")
        if consider:
            try:
                pl = json.loads(gj.read_text())
                slice_pipeline_consider(str(pl.get("job_id")), pl, gj)
            except Exception:
                pass
        return 0


if __name__ == "__main__":
    # --self-test unit-tests the pure readers (no GPU/state/network); checked
    # before argparse so it needs no --job-id.
    if "--self-test" in sys.argv[1:]:
        # The self-test must be HERMETIC w.r.t. GATE_TEST_MODE: its fixtures are
        # written for TEST_MODE=False (the no-env default), injecting fake
        # runners/enqueuers wherever a live effect would happen, and the sections
        # that need TEST_MODE=True set and restore it themselves. Inheriting
        # GATE_TEST_MODE=1 from a harness (signoff-canary, test-*.py) silently
        # disabled the live triage/auto-apply paths under test, so those checks
        # FAILed or crashed (KeyError 'escalation_triage_status').
        TEST_MODE = False
        sys.exit(0 if _self_test() else 1)
    sys.exit(_guarded_main())
