# v10 — PREREGISTRATION (cell E, bulk/mechanical codemod)

**Status: RULED and REBUILT — Fable returned REBUILD 2026-08-25 ~15:50; the fixture was rebuilt to
all five amendments and re-pinned at `b2d9b21`.** §9b carries the amendments; §2 the verified result. Written while the catch-up two-turn round held the GPU;
the rebuild is CPU-only and fits the same window.

Governing gate, from the v10 plan: *"Order stays E -> C -> D -> B, B cut first. Next gate:
preregistration sign-off before dispatch."* This is that document. Nothing in cell E dispatches
until the rebuilt fixture is re-pinned, its hash recorded here, and every §8 item is true.

---

## 1. What cell E measures

The bulk/mechanical work class the owner most wants to offload, currently at **n=0 coverage**. A model is
given a frozen repo snapshot and a codemod spec in prose, and must apply every replacement across the
tree. Scoring is a **byte-diff against a deterministic oracle** — not a judge, not a test suite, not a
self-report.

**The primary question:** can a model apply a mechanical multi-site change correctly and completely,
without over-escaping regex metacharacters and without claiming edits it did not make?

**The trap this cell exists to carry.** `qwen3-14b-agentic` failed in v9 by sending
`r'-(\[A-Za-z\]+)$'` against a file containing `r'-([A-Za-z]+)$'` — it escaped the bracket class that
was already literal, then reported the edit as applied. That is the only failure mode this programme
has caught in the wild, and a bulk cell without metacharacter-bearing targets would miss it.

## 2. Fixture — VERIFIED, pinned

`bakeoff-fixtures/bulk-codemod-v10`, git-pinned **`b2d9b21`** (rebuilt; was `ea5c773`), working tree clean at time of writing.
A frozen composite; **never the live repos**. Every figure below was re-derived from the artifact for
this document, not copied from the handoff.

| property | value | approved spec | met? |
|---|---|---|---|
| source files present | 20 (6 py + 14 ts) | — | — |
| total lines | 10,011 | — | — |
| replacement sites | **49** | 40–60 | yes *(was 39)* |
| metacharacter sites | 23 (**47%**) | ≥1/3 | yes *(was 15 / 38%)* |
| **files carrying targets** | **12** | **12–20** | yes *(was 6)* |
| idempotent | yes | required | yes |
| byte-stable | yes | required | yes |

**Idempotence is verified, not asserted:** `python3 codemod.py --verify-idempotent` copies the tree,
applies twice, and compares SHA-256. Pass 1 = 49 sites, pass 2 = **0 sites**, tree hash **`30a06c4a0d2f4537`** identical
across both passes. Re-runnable; the fixture is left clean.

**⚠ The tree hash was not reproducible, and is now fixed (`4ffb0dd`).** Two reviewers reported
different hashes for the *same* pinned commit — `05c6cafccad3816d` and `6d4970e83702be22` — and both
were right. `tree_hash()` walked everything under the root, so importing `codemod.py` to inspect its
replacement table created `__pycache__/` and changed the hash. **A tree hash sensitive to bytecode
caching cannot certify byte stability, which is this oracle's headline property and what the round's
ground truth rests on.** `__pycache__/`, `*.pyc`, `*.pyo` are now excluded alongside `.git` and
`CHECKSUMS.sha256`, and a `.gitignore` prevents the cache being committed. Verified by the control
that would have falsified the fix: `__pycache__` deliberately recreated, hash identical either way
(`b0e87cde163b6900` at `4ffb0dd`).

**Open at re-pin, deliberately not decided unilaterally:** `codemod.py` is itself inside the hashed
tree, so any oracle edit moves the fixture hash. Harmless for idempotence (it does not change between
passes) but it conflates *"the fixture moved"* with *"the oracle changed"*.

**The oracle cannot be wrong the way the models are wrong.** Every entry is an exact literal string
match, never a regex substitution (`codemod.py:9-12`). A regex-based oracle would be subject to the
same escaping bug this cell measures, and an oracle that fails like the thing it grades is not an
oracle. Idempotence is guaranteed by construction — for every pair, `old` does not occur inside `new`,
asserted at `codemod.py:98`.

**A dead target is fatal, not a warning** (`codemod.py:110-121`). An earlier draft of the replacement
table guessed four literals; three silently matched nothing, and the codemod still ran clean and still
reported IDEMPOTENT, because "replaced 0 occurrences" is indistinguishable from "nothing to do". That
is this cell's own failure mode occurring inside the oracle. `apply()` now raises on any zero-site
target. **This guard is the single most important line in the oracle and must not be relaxed to make a
revised fixture pass.**

## 3. Site distribution — REBUILT

| file | sites | kinds |
|---|---:|---|
| `py/arr-webhook.py` | 13 | ident, meta |
| `py/test_release_group.py` | 8 | ident |
| `py/test_maintenance.py` | 7 | ident |
| `ts/csvParsers.ts` | 4 | meta |
| `ts/emailSync.ts` | 4 | meta |
| `ts/cardcenter.ts` | 4 | meta |
| `ts/orderReturns.ts` | 2 | ident |
| `ts/secrets.ts` | 2 | ident |
| `ts/bfmrJoin.ts` | 2 | meta |
| `ts/returnStatus.ts` | 1 | ident |
| `ts/carrier.ts` | 1 | meta |
| `ts/bfmrWeb.ts` | 1 | meta |
| **8 other files** | **0** | inert, deliberately retained |

**Before the rebuild** this was 39 sites in 6 files with `arr-webhook.py` at 51%. It is now **49 sites
across 12 files, `arr-webhook.py` at 26.5%**, with the 8 inert files kept on Fable's ruling that decoy
discipline is real work — targets must not be spread across all 20.

Two **cross-file** renames were added on purpose (`torrent_is_unregistered` across 2 Python files,
`isFullyReturned` across 2 TypeScript files): applying a rule consistently in file 1 and still applying
it in file 2 is the coordination load the cell is named for, and the retired `remove_torrent` had been
the only multi-file rename.

## 4. Oracle and scoring

- **Ground truth:** the tree produced by `codemod.py` applied to a pristine copy at `b2d9b21`.
- **Score:** byte-diff of the model's tree against the oracle tree. Per-site credit; a site is correct
  only if the bytes match exactly.
- **Reported per run:** sites correct / 49, sites missed, sites wrongly modified, files touched that
  the oracle does not touch (collateral damage), wall-clock, tokens, iterations, stop reason.
- **Metacharacter sites are scored and reported separately from ident sites.** Pooling them would hide
  the exact effect the cell was built to detect.
- **No claim-vs-verify matcher in this cell** (Fable Q5). The byte-diff catches a fabricated claim
  mechanically, so the matcher is redundant here.

## 5. Harness calibration — 2 runs, 0 models (Fable Q5)

The threat to this cell is **an oracle bug silently passing everyone**, so the oracle is calibrated
before any model runs:

1. **Known-good:** hand-apply `codemod.py` to a pristine copy; the scorer must return a perfect score.
2. **Known-bad:** introduce exactly one deliberately wrong replacement; the scorer must catch it and
   name the site.

**If either calibration run fails, cell E does not dispatch.** These are harness runs and consume no
model GPU time.

## 6. Roster and per-model configuration

Equal **information**, not equal plumbing (the owner's governing principle). Identical across every model:
**task text, fixture state, verify command, read cap, tool surface, scoring.** May vary per model:
**backend, chat template, system-prompt scaffold, sampling, context window.**

Context is **capability**, not information (Fable's ruling): a `config_ceiling` is a bug in how we ran
it and is re-runnable; a `native_ceiling` is a real measurement of the model and is keepable.

Roster is inherited from the catch-up round's outcome and is **not fixed by this document** — see §9.

## 7. Pre-registered exclusions and degenerate cases

Fixed now so they cannot be invented after seeing results.

- **`NO_OUTPUT`** — the model changed no files. Recorded, scored **0/49**, and **not** excluded. Unlike
  the two-turn cell, producing nothing here is a genuine failure at the task, not a missing precondition
  for a second turn. *(Fable: confirmed.)* **Boundary condition Fable added:** 0/49 applies only when the
  model **demonstrably completed a run** and produced no changes. A harness failure — crash, OOM, never
  invoked — is a **rerun, not a zero**, and the dispatch log must distinguish the two.
- **`HARNESS_ABORT`** — the run died for a harness reason (worker crash, OOM, server restart). Excluded
  from scoring, re-run once, and the exclusion is reported.
- **`ORACLE_DISAGREEMENT`** — the model's tree differs from the oracle in a way that is arguably also
  correct (e.g. a semantically equivalent regex). **Adjudicated, not silently scored either way.** The
  metacharacter targets are exactly where a defensible alternative spelling is most likely.
  *(Fable: human adjudication is the right call — mechanical regex-equivalence cannot be pre-defined
  completely, and a partial mechanical definition silently mis-scores everything outside it. Tightened
  three ways:)*
  - **(a) The byte-diff score is primary and is never overwritten.** Adjudication produces a **separate
    annotated column** ("semantically-defensible deviations"), so raw and adjudicated numbers are both
    always reported. The cell's construct is *mechanical fidelity*: a model that rewrites a regex into
    an equivalent spelling has failed to copy mechanically even where it has not failed semantically.
  - **(b) Adjudication is blind** — model identity is stripped from the diff before it is reviewed.
  - **(c) Pre-committed now, in writing:** escaping an already-literal bracket class (`\[...\]` for
    `[...]` — the exact v9 `qwen3-14b-agentic` pattern) **changes the matched language and is NOT
    equivalent.** Recorded here so the trap this cell exists for can never be adjudicated away.
- **Collateral edits** are reported but do **not** reduce the site score; they are a separate column.
  Pre-registered so the temptation to fold them into one headline number is foreclosed.

## 8. Pre-dispatch checklist (nothing dispatches until all true)

1. [x] **Fable rules on §9.** — **RULED REBUILD**, 2026-08-25 ~15:50. See §9b.
2. [x] **Fixture REBUILT to the §9b amendments** — `b2d9b21`. **49 sites / 12 carrying files / 8
       inert; `arr-webhook.py` 13/49 = 26.5%; meta 23/49 = 47% across 7 files incl. 6 TypeScript.**
       All ten constraints verified mechanically, not by eye.
3. [x] Fixture re-pinned at **`b2d9b21`**, tree clean, `CHECKSUMS.sha256` regenerated — and it
       regenerates **identically**, because the fixture *content* never changed; only the oracle's
       replacement table moved.
4. [x] `--verify-idempotent` passes on the rebuilt fixture: pass 1 = 49 sites, pass 2 = **0 sites**,
       tree hash **`30a06c4a0d2f4537`** identical across both passes, with the §2 hash fix
       (`4ffb0dd`) in place so it reproduces across machines.
5. [ ] Both §5 calibration runs pass.
6. [ ] Worktrees **fresh**, ancestry verified per cell, abort on failure.
7. [ ] Every worktree has deps installed — **worktree env parity has burned a real dispatch before.**
8. [ ] Driver writes to `results-v10-bulk.csv`. **A cloned driver with a hardcoded results path has
       already nearly appended into a frozen dataset once** (`ONBOARDING-A-MODEL.md`, silent-failure
       gate 1). Assert the path before the first run.
9. [ ] Scorer is handed **all** result dirs at once, never one at a time — handing it one makes each
       model its own ground truth and produced three wrong numbers before being caught (silent-failure
       gate 2).
10. [ ] Per-model native `tool_call` smoke check on the dispatch host (Fable's mandatory per-model gate).
11. [ ] `num_ctx` verified via `/api/show` on the **exact tag dispatched**, not the modelfile assumed.

## 9. The fixture as built does not implement the approved spec — RULED, see §9b

Fable approved: **"40–60 replacements across 12–20 files"**, ≥1/3 metacharacter-bearing.

Built: **39 sites across 6 files**, 38% metacharacter.

The metacharacter ratio is met. The site count is one short. **The file spread is half the approved
floor**, and that is the deviation that matters — the other two are rounding.

**Why this is not cosmetic.** The cell is named *bulk/mechanical* and exists to measure multi-file
mechanical work. With 51% of sites in one file and 14 of 20 files inert, the fixture largely measures
**concentrated single-file find-and-replace surrounded by decoy context** — a different and easier
task. The multi-file coordination load, which is the part the owner actually wants to offload, is mostly
absent. Twelve of the fourteen TypeScript files carry nothing, so the cell is also far more a Python
exercise than the composite design implies.

**How the number misleads.** "20 files, 39 sites" reads as *39 sites spread across 20 files*. It is
*39 sites across 6 files, with 20 files present*. Files-present and files-edited are different
quantities and the spec constrains the second.

**Recommended remedy** (CPU-only; can be done now, while the GPU is busy):
add literal targets in **6–8 of the inert TypeScript files** to reach **12–14 files carrying targets
and 45–50 sites**, holding metacharacter share ≥1/3 and keeping `arr-webhook.py` under ~30% of sites.
Then re-run `--verify-idempotent`, re-checksum, re-pin, and update §2 here.

**Every candidate literal must be probed against the frozen fixture before it enters the table** — the
dead-target trap in §2 caught three guessed literals once already, and the strict guard must do the
catching, not a reviewer.

## 9b. FABLE'S RULING — **REBUILD** (2026-08-25 ~15:50)

Fable independently re-verified every figure against `ea5c773` and confirmed the per-file sums exactly.
**Cell E does not dispatch against `ea5c773`.**

**The reasoning, which went further than mine:** with 51% of sites in one Python file and **72% in
two**, the task a model actually experiences is one heavy find-and-replace, a moderate one, four small
touches, and restraint elsewhere. The failure modes bulk codemod work is genuinely prone to — *losing
the thread across many files, applying a rule consistently in file 3 but drifting by file 12, silently
stopping partway through the tree* — barely get exercised. Separately and sufficiently: **dispatching
with the miss recorded as a "stated limitation" is the quiet-goalpost move preregistration exists to
prevent.** When honouring the preregistration is nearly free, you honour it.

**On the decoy-context counterargument (Fable was asked to steelman it): "partially right."** The 14
inert files are real work, and collateral damage is already scored. But *decoy discipline and
multi-file application consistency are different constructs* — a model with competent search collapses
the inert files to a cheap grep pass and the task degenerates to "edit 6 files, mostly 2." This earns
the inert files **a place in the rebuilt fixture — targets must NOT be spread across all 20** — but
does not rescue GO.

### Binding amendments

1. **Spread:** 12–14 files carry ≥1 site; **45–50 total sites**; 6–8 files remain fully inert.
2. **Concentration cap: `arr-webhook.py` ≤ 30% of total sites.** ⚠ **Arithmetic my recommendation got
   wrong:** 20 arr-webhook sites cannot sit under 30% of any total ≤66, so the rebuild must **retire
   arr-webhook `(old, new)` pairs** down to ~13–15 sites — not merely add TypeScript sites. **Retire
   whole pairs only.** Never keep a pair while exempting some of its occurrences, or the oracle
   contradicts the "replace every occurrence" prose spec. Retired strings stay in the file as natural
   decoys.
3. **Metacharacter distribution:** share stays ≥1/3 of total, **and meta sites must appear in ≥4 files
   including ≥2 TypeScript files.** Currently 7 of 15 meta sites are in `arr-webhook.py` — **the
   over-escaping trap must not be Python-local**; TS regex literals carry the identical trap.
4. **Probe before entry.** Every candidate literal is probed against the frozen fixture before entering
   the table. **The dead-target strict guard is not relaxed under any circumstance** — if a revised
   table trips it, the table is wrong, not the guard.
5. **Re-pin procedure:** `--verify-idempotent` clean → regenerate `CHECKSUMS.sha256` → commit → record
   the new commit hash in this document. The tree-hash defect in §2 had to be resolved first; it was
   (`4ffb0dd`).

## 10. Budget

| phase | dispatches | note |
|---|---|---|
| harness calibration | 0 | 2 harness runs, no model time |
| cell E | roster × n=3 | sized once §9 is ruled and the roster is fixed |
| C (`qwen3-coder:30b` probe) | conditional n=1 @131072 | gated on the 3-part preflight |
| D (`qwen2.5-coder:14b` YaRN) | per v10 plan | unchanged |
| B (devstral, backend column) | per v10 plan | **cut first** if GPU budget runs short |

## 11. Falsification criteria (pre-registered) — round invalid if any fire

1. Either §5 calibration run fails.
2. Any `timed_out=true`.
3. Any missing token counts, or any `decode_s=0`.
4. Any row stopping `config_ceiling` — the read cap did not work and the cell is measuring context
   size, not codemod ability.
5. The oracle's `--verify-idempotent` does not reproduce byte-identically at scoring time as it did at
   pin time — the ground truth moved under the round.
6. Any dead target in the replacement table at dispatch time (`apply()` strict guard raising).
7. Scorer run against fewer than all result dirs.
