#!/usr/bin/env python3
"""Recompute routing-table evidence from the frozen result CSVs.

GENERATED, NOT HAND-EDITED, for the same reason the cell-E task text is: a
hand-maintained table drifts from the data it claims to summarise, and nobody
notices because both look plausible. The current routing.json says
`n_rows: 69, generated: 2026-08-24` and is three rounds stale.

WHAT THIS TOOL DECIDES: the EVIDENCE -- how many valid measurements exist per
model per work class, and how many of them succeeded under a criterion that can
actually tell success from inaction.

WHAT IT DOES NOT DECIDE: the VERDICT (`unsupervised` / `harness-verified` /
`scaffolded` / `don't dispatch`). Verdicts are a design call and belong to Fable.
This emits `verdict: null` with the evidence attached, so a verdict is never
silently inherited from a stale row after its evidence has moved.

THE DISCRIMINATING-CRITERION PROBLEM
------------------------------------
`verify_passed` is only a success signal on a cell whose verify FAILS on the
pristine tree. Clamshell qualifies -- but NOT because its preflight must fail
(it does not; `PREFLIGHT_MUST_FAIL` is set only for the debug cell). It
qualifies because its verify is strictly STRONGER than its preflight: preflight
is `swift build`, verify adds `swift run Clamshell confirmation-bridge-selftest`,
and that selftest cannot pass while ConfirmationBridge is absent. The photo cell
uses the SAME command for both, and the preflight must PASS, so a model that
changes nothing passes verify. Four historical rows
are exactly that, and two sat inside shipped verdicts.

So photo rows are scored on `verify_passed AND files_changed > 0`, which is a
floor rather than a fix -- it catches inaction, not shallow work. The real fix is
the v11 retest, which archives the created files so the work can be inspected.
"""
import csv
import json
from collections import defaultdict
from pathlib import Path

D = Path("/Users/user/Desktop/GitHub Projects/bakeoff/model-buildoff-2026-08-22")
VOID = {("results-v8.csv", "plex-release-group-debug")}

CELLS = {
    "debugging_from_symptom_report": ("debug", True),
    "greenfield_feature_large_ts_repo": ("photo", False),
    "api_recall_dependent_swift": ("clamshell", True),
}


def evidence_tier(n: int) -> str:
    """How much weight a row can bear, carried in the row itself.

    Fable's correction: without this, a single-round 3/3 renders with the same
    typography as a much better-evidenced row and silently reads as settled.
    n=3 is a floor of evidence, not a ceiling of confidence.
    """
    if n == 0:
        return "none"
    if n <= 2:
        return "anecdote"        # cannot separate capability from luck
    if n <= 3:
        return "single-round"    # one round, one host, one roster
    if n <= 6:
        return "replicated"
    return "multi-round"


def valid(fname, r):
    for vf, vt in VOID:
        if fname == vf and vt in r.get("task", ""):
            return False
    blob = (r.get("stop_reason") or "") + "|" + (r.get("exit_code") or "")
    if "ABORT" in blob or "config_ceiling" in (r.get("stop_reason") or ""):
        return False
    if (r.get("timed_out") or "").lower() == "true":
        return False
    return True


def main():
    ev = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for f in ["results-v8.csv", "results-v9.csv", "results-v10catchup.csv",
              "results-v10parity.csv"]:
        p = D / f
        if not p.is_file():
            continue
        for r in csv.DictReader(open(p)):
            if (r.get("arm") or "base") != "base":
                continue
            if not valid(f, r):
                continue
            t = r.get("task", "")
            cell = ("photo" if "photo" in t else "clamshell" if "clamshell" in t
                    else "debug" if "debug" in t else None)
            if not cell:
                continue
            m = r["model"]
            ev[cell][m][1] += 1
            passed = r.get("verify_passed") == "yes"
            if cell == "photo":
                passed = passed and int(r.get("files_changed") or 0) > 0
            if passed:
                ev[cell][m][0] += 1

    # two-turn recovery
    rec = defaultdict(lambda: [0, 0])
    for f in ["results-v9-r3.csv", "results-v10catchup-twoturn.csv",
              "results-v10parity-tiebreak-twoturn.csv"]:
        p = D / f
        if not p.is_file():
            continue
        for r in csv.DictReader(open(p)):
            if r.get("turn2_outcome") != "RAN":
                continue
            rec[r["model"]][1] += 1
            if r.get("turn2_verify") == "yes":
                rec[r["model"]][0] += 1

    out = {}
    for cid, (cell, discriminating) in CELLS.items():
        entries = []
        for m, (p, n) in sorted(ev[cell].items()):
            entries.append({
                "model": m, "verdict": None, "status": "pending_fable",
                "passes": f"{p}/{n}",
                "n": n,
                "evidence_tier": evidence_tier(n),
                "criterion": ("verify_passed" if discriminating
                              else "verify_passed AND files_changed>0"),
            })
        out[cid] = {
            "id": cid, "status": "pending_fable", "entries": entries,
            "verify_discriminating": discriminating,
        }
        if not discriminating:
            out[cid]["caveat"] = (
                "The verify for this cell PASSES on the pristine tree, so it "
                "cannot distinguish building the feature from doing nothing. "
                "Scored on verify_passed AND files_changed>0, which catches "
                "inaction but not shallow work. The files the models created "
                "were never archived, so the work was never inspected. "
                "Superseded by the v11 retest.")

    out["followup_on_own_prior_output"] = {
        "id": "followup_on_own_prior_output", "status": "pending_fable",
        "verify_discriminating": True,
        "note": ("Previously not_measured on the grounds that the naive design "
                 "leaks the answer. The R3 driver avoids that: turn 2 receives "
                 "the VERBATIM harness verify output, never a human description "
                 "naming the function or mechanism."),
        "entries": [{"model": m, "verdict": None, "status": "pending_fable",
                     "passes": f"{p}/{n}", "n": n,
                     "evidence_tier": evidence_tier(n),
                     "criterion": "turn2_verify"}
                    for m, (p, n) in sorted(rec.items())],
    }
    tp = sum(v[0] for v in rec.values())
    tn = sum(v[1] for v in rec.values())
    out["followup_on_own_prior_output"]["aggregate"] = f"{tp}/{tn}"

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
