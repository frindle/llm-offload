# v9 design v2 — post-Fable, for second go/no-go (2026-08-24)

Consolidated after Fable's NO-GO on v1. Every v1 blocker is resolved below; the quality arm is descoped
to v10 per Fable. Original v1 draft + Fable's full report live in `Ollama-v9-Design.md` (audit trail).

## What v9 is
The **rerun round**: repair the v8 cells whose data is invalid because the *instrument* broke, plus the
one cell v8's preregistration deferred. v8 itself passed all four §5 falsifiers and is NOT redone.

## Rescore (zero dispatch) — DONE
`bakeoff-v8-score.py` `_WORKER_PATH` repointed to `ollama-worker-v7.py` (the worker `bakeoff-v8-lib.sh:69`
actually ran), re-run: 69 rows reproduce the documented corrected figures. Also added a **read-pattern
scorer** (per Fable Q4 #5): per-run classification `grep_first / paged / windowed_top / reread_top /
no_read`, so any residual `config_ceiling` in R1 is diagnosable and we can tell whether the cap or the grep
hint did the work.

---

## R1 — debug cell (`plex-automation`), all 18 rows

v8's debug cell measured "the file doesn't fit in context," not debugging (`arr-webhook.py` 5034 lines /
228KB; `tool_read_file` had no cap; 10/18 rows stalled `config_ceiling` having read nothing usable).

**Instrument fix — DONE + re-smoked (was B1):** `tool_read_file` now defaults to a **600-line window**
(was 2000, which was still ~23k tokens and ceilinged the 32k/41k models once history is added), with
`offset`/`limit` paging and a grep-to-symbol hint, in both worker copies. Re-smoked against the real
`arr-webhook.py` with **cumulative-context arithmetic**: default read ~8k tok; system prompt (~724) +
tool schemas (~1123) + task + **two** sequential reads + one edit turn ≈ **21k tok, fits qwen2.5-coder's
32k with ~11.8k headroom**. Whole files that already fit return verbatim (small-file behaviour unchanged).

**Rerun all 18, not 10** (Fable Q1 confirmed): changing the instrument changes the cell; pooling pre/post-
fix rows is a two-harness comparison reintroducing v8 §3's confound; and §1's "any single inert at n=3
disqualifies" can't apply across reps run under different harnesses. The 8 genuinely-engaged v8 rows
(incl. qwen2.5-coder's port 5001-5020 doom-loop — possibly an artifact of the hopeless-read world; its
behaviour with a working read_file is the open question) are **archived as valid v8 observations, reported
separately**.

---

## R2 — `qwen3-coder:30b` clamshell base r3, 1 row

Emitted Qwen-native `<function=list_files>` XML nothing parsed → 0 executed calls, scored as inert.

**Instrument fix — DONE:** `extract_qwen_xml_tool_calls` parses `<function=NAME><parameter=K>V</parameter>
</function>`, wired as a JSON-first fallback in both workers (no double-count; unit-tested). Re-dispatch the
single rep. Do NOT apply §1's inert disqualifier to this cell until after the rerun.

**New R2 falsifier (Fable Q4 #2):** unit tests passing ≠ the wired fallback firing under a live run. If the
R2 rerun transcript contains `<function=` XML calls but the executed-call count is still 0, the parser
failed in situ and the row is **instrument-invalid again** — do not score it as a model result.

---

## R3 — "follow-up on own prior output" (the pre-planned `NONE — v9` cell)

Two-turn cell. Turn 1: an ordinary task from an existing cell. Turn 2: the model is given its own turn-1
output plus a real defect in it, and asked to fix it.

**Defect delivery is mechanical, verbatim, zero human prose (was B2 / Fable Q2):** the turn-2 defect is
**the verbatim failing verify/test output captured by the harness** — never a human description naming the
function or mechanism (that is an answer key by another route, and its helpfulness varies per model,
destroying comparability). "Who identifies the defect": the harness does, identically for every model.

**Pre-registered outcomes for the degenerate cases:**
- `NO_TURN1_OUTPUT` — turn 1 produced nothing usable; no turn 2 runs. Excluded from every turn-2 denominator.
- `NO_DEFECT_FOUND` — turn-1 output exists and **passes verify**; no real defect to feed, none is invented;
  no turn 2 runs; excluded from every turn-2 denominator.

**Turn-2 token-budget check (pre-dispatch):** turn-1 output + verbatim defect text + prompt must fit every
model's window, or R3 recreates R1's overflow. Verified before dispatch.

**Deliverable is a behavioral CLASSIFICATION, not a fix-rate ranking (Fable Q2):** each model faces its own
defect of its own difficulty, so cross-model fix-rate is not comparable. Pre-registered classes: *revises
own work / re-derives from scratch / defends-or-denies the defect / fixes it.* Written down now so the
tempting invalid post-hoc reading (a fix-rate leaderboard) is foreclosed.

---

## Quality arm — DESCOPED to v10 (was B3 / Fable Q3)

Cannot run as v1 specified: (a) the provenance falsifier is near-certain to fire — a human shipped commit
is a minimal diff in the repo idiom, a model impl from symptom text is structurally distinct (scaffolding,
comment density, over-completeness), so reviewers beat 50% on style regardless of quality and falsifier 6
then voids all scores — the arm was designed to invalidate itself; and (b) **reviewer memory
contamination**: Fable/Opus routinely work in these exact repos with project memory loaded, so a reviewer
may *recognize* the shipped commit, which the provenance check can't distinguish from style detection.

**v10 redesign (recorded now so it isn't relost):** normalize both artifacts (strip comments, uniform
format, both as diffs vs the same parent); score **correctness by the repo's own tests**, not reviewer
preference; reserve blind review for clarity/production-readiness only, with the provenance-guess rate a
reported caveat not a binary invalidator; run reviews in **fresh-context reviewer sessions with no repo
memory**, preferring commits those sessions provably never touched; pre-register the pair count and the
actual statistical test (at single-digit pairs, "above chance" is otherwise undefined). The owner wants quality
testing — this is deferred, not dropped.

---

## Falsification criteria (pre-registered)
Round invalid if any fire:
1. `host_ready=no` > 20% of reachable-gate cells (excluding STRUCTURAL `qwen3.8`, `qwen3-coder-next`).
2. Any `timed_out=true`.
3. Any missing token counts or any `decode_s=0`.
4. **R1**: any row still stopping `config_ceiling` — the read cap did not work; the cell still measures
   context size, not debugging.
5. **R2**: XML calls present in the rerun transcript but executed-call count still 0 — parser failed in
   situ; row instrument-invalid.
6. **R3**: turn-2 defect found to be anything other than verbatim mechanical verify output.

## Pre-dispatch checklist (nothing dispatches until all true)
1. Rescore complete + behaviour CSV re-emitted. **[done]**
2. `read_file` cap at 600 lines, re-smoked against `arr-webhook.py` with cumulative-context arithmetic.
   **[done]**
3. Qwen-XML parser in the worker, unit-tested by the permanent, re-runnable
   `test-worker-parsers.py` (17 checks incl. XML parse, JSON-first, read-cap paging, v7==v8 parity). **[done]**
4. B3 host-state exercise passes (as v8).
5. **Worktrees are FRESH** and ancestry verified per cell — mechanically, abort on failure. (v8's 10
   stalled R1 runs left junk files behind; a reused worktree carries them.)
6. **Inverted preflight per R1/R3 worktree**: the symptom must REPRODUCE on the pristine tree before
   dispatch (v8 debug-cell pattern). Absence-of-a-commit ≠ presence-of-the-symptom (reverts, re-lands,
   partial fixes), so reproduce-or-abort, not just ancestry.
7. Every worktree has deps installed (worktree env parity has burned a real dispatch before).
8. Driver confirmed to run the intended worker (`ollama-worker-v7.py`, per `bakeoff-v8-lib.sh:69`), and a
   **checksum assert that v7 == v8** across `extract_manual_tool_calls`, `tool_read_file`, AND
   `extract_qwen_xml_tool_calls` (the newest code, likeliest to drift — Fable v2 nitpick #2) in the
   dispatch script, so silent drift can't reintroduce the scorer/worker split. (Fable Q4 #6.)
   `test-worker-parsers.py` already asserts this parity and can be the check.
9. R3 turn-2 token-budget check passes for every model.

## Budget
| phase | scope | note |
|---|---|---|
| rescore + read-pattern scorer | 0 dispatches | done |
| R1 | 18 runs | 6 models × 3 reps, debug cell only |
| R2 | 1 run | single rep |
| R3 | TBD | 2 turns/rep; size once the cell is built |
| quality arm | — | moved to v10 |

## Between v9 and v10 — pre-registered two-checkpoint gate
Not one review — two, because they differ in kind:
1. **v9 results-read (analysis).** Read v9's output against its own falsifiers. Does double duty: (a)
   confirms the instrument fixes worked — the `read_pattern` column shows whether R1 models grep-and-
   windowed or still choked; falsifier #5 shows whether the R2 XML parser fired in a live run, not just in
   unit tests; if either didn't take, R1/R2 are still invalid and v10 is premature. (b) Sets v10's roster —
   v9 says which models are worth the expensive blind-review arm; don't quality-test a model v9 shows inert.
2. **v10 design go/no-go (Fable), before any blind review runs.** The quality arm is a brand-new,
   unvalidated instrument (LLM-judge) — this project's one recurring failure mode. Its redesign (above) is a
   sketch that must be fully designed and reviewed first, including the setup work: selecting commits the
   reviewing sessions provably never touched and provisioning fresh-context reviewer sessions with no repo
   memory. That prep belongs IN the v10 design, reviewed with it, not bolted on after a GO.

## Deliberately not doing
- Not re-running any sound v8 cell. Not changing roster/gates/ceiling-split/wall-clock/randomisation.
- Not pooling pre/post-fix debug rows. Not building the quality arm in v9.
