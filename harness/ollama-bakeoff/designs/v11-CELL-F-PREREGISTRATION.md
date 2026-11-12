# v11 cell F — PREREGISTRATION (instrument construction)

**Status: DRAFT, unsigned. Queued behind v10 cell E.** the owner's idea, 2026-08-25: send prep work for the
next round to the models themselves and score them on it.

---

## 1. Why this cell can exist when the quality arm could not

Most prep work has **no ground truth**. "Design the next round", "write the results-read" — grading
those needs judgement, judgement means an LLM judge, and an LLM judge is the instrument failure that
got the v9 quality arm descoped (the provenance falsifier fires near-certainly; reviewer memory
contamination is unfixable in-house). Reintroducing a judge through the side door would repeat it.

**Fixture construction is the exception, and it is the only prep task in this cell.** Every property
that makes a bulk-codemod fixture valid is decidable by a script:

- either every literal matches **exactly once** or it does not
- either the transform is **idempotent** and byte-stable or it is not
- either the sites are spread across 12–14 files or they are not

**There is nothing to adjudicate.** No judge, no human read, no confidence field.

## 2. The task

Given a **real repository the model has not seen in this programme** (candidates: `clamshell` Swift,
`network-bandwidth-monitor` Python, `meshtastic-bitchat-bridge`), produce a frozen composite fixture
plus a `codemod.py` oracle satisfying the published constraints.

The model receives the **same constraint list the scorer enforces**. This is not a hidden-rubric test —
withholding the spec would measure guessing. The difficulty is not knowing the rules; it is **applying
them to an unfamiliar codebase without producing a dead target**.

## 3. The oracle — `bakeoff-fixture-check.py`

Ten mechanical constraints. **Already validated against historical artifacts**, which is the strongest
calibration available because these are real fixtures, not synthetic ones:

| fixture | result |
|---|---|
| `bulk-codemod-v10` @ `b2d9b21` (post-rebuild) | **10/10 — VALID** |
| `bulk-codemod-v10` @ `ea5c773` (pre-rebuild) | **5/10 — INVALID** |

The pre-rebuild failures are *exactly* the constraints Fable independently ruled on: total sites,
files carrying, inert count, concentration (51.3%), and meta-file spread (3 of a required 4). **The
checker rediscovers Fable's amendments from the artifact alone**, without being told them.

**Score = constraints met / 10**, plus a hard gate: **a fixture with a dead target scores zero
regardless of the other nine.** A dead target means the oracle silently grades against a transform it
never applied, so every model scores perfect on those sites — the defect that would corrupt a whole
round, and the one this programme has already hit once inside its own oracle.

## 4. What the oracle does NOT check — stated so it cannot be overclaimed

Whether the fixture is **interesting**: whether its targets exercise a failure mode worth measuring.
That is a judgement call and it stays with a human. **A fixture can pass all ten checks and still be a
bad test, so a PASS here is a floor, never an endorsement.**

## 5. ⚠ SECURITY GATE — this cell trips a pre-registered trigger

`Claude/Projects/open-model-security-review.md` is parked, with the trigger recorded as:

> *before any of these models touches anything that is not a throwaway worktree.*

**Programme infrastructure is not a throwaway worktree.** The surface is the Jinja chat template, the
Modelfile, and the fact that **the harness executes model tool calls with full privileges**. So:

1. The security review comes off the parked pile **before this cell dispatches**, OR
2. the cell runs in a sandbox whose output is reviewed before anything is adopted.

**Either way, output is scored but NEVER adopted unreviewed.** A model-built fixture is an unvalidated
instrument by construction, and unvalidated instruments are this programme's single recurring failure
mode. Tonight alone produced four instrument defects, all caught by hand: an **answer-key leak** (the
fixture repo ships `codemod.py`, so handing a model a worktree of it would have let it score 49/49
having done nothing), a dead-target trap, a tree hash contaminated by `__pycache__`, and a literal
U+00A0 in a replacement literal. A model doing this work needs the *same* scrutiny, so **this cell does
not reduce review cost and must not be sold as if it does.**

## 6. Second candidate task — mechanically gradeable, lower value

**Environment preparation:** create N worktrees at a pinned baseline with dependencies installed such
that the verify command passes. Pass/fail on the build; no judgement.

Worth measuring because it is the gate that **burned two dispatches in one evening** — the photo cell
aborted 9 runs for missing worktrees, and `npm ci` alone was insufficient because the Prisma client is
generated rather than installed. Included as a secondary cell only if budget allows.

## 7. Pre-registered exclusions

- **`NO_OUTPUT`** — no fixture produced: scores **0/10**, not excluded. Producing nothing is the failure.
- **`HARNESS_ABORT`** — excluded, re-run once, exclusion reported.
- **No `ORACLE_DISAGREEMENT` class.** Unlike cell E there is no defensible-alternative-spelling
  question: the constraints are numeric thresholds and a boolean dead-target check. Nothing to
  adjudicate, so nothing is adjudicable.

## 8. Falsification criteria — round invalid if any fire

1. The checker fails its own calibration (must return 10/10 on `b2d9b21` and 5/10 on `ea5c773`).
2. Any `timed_out=true`.
3. A model's fixture passes all ten checks but a human review finds it targets nothing meaningful —
   **this would show the oracle is necessary but not sufficient**, and the cell's scoring would need
   redesign before any verdict rests on it.
4. Security gate (§5) not satisfied at dispatch time.

## 9. Open for Fable

- Is fixture construction a **legitimate work class** for the routing table, or is it programme-internal
  work that should not carry a dispatch verdict at all?
- Does a **10-constraint pass/fail** support any verdict beyond `harness-verified`? My read: no —
  §4 means a PASS is a floor, so `unsupervised` is not reachable on this cell by construction.
- Does the security review need to complete first, or is a sandboxed run with mandatory human review
  sufficient to satisfy the trigger?
