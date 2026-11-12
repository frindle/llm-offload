#!/usr/bin/env python3
"""harness-repair-verify.py -- acceptance check of an in-place HARNESS REPAIR job.

Used as (the tail of) the repair job's --verify:
    python3 auto-harness-check.py && python3 ~/bin/harness-repair-verify.py <worktree> [--require=LIT ...]

Passes (prints HARNESS_REPAIR_OK, exit 0) only when the relevance gate -- the
preflight, the same authority `--regate` consults -- reports verdict `relevant` (no
verify-relevance blocker, no blocking survivor). Otherwise it prints the survivors that remain (so the
model gets the exact feedback on its next iteration) and exits 1. The worker's own
VERIFY_OK requirement (auto-harness-check.py) is the first half of the `&&`.
It never edits anything but what the preflight's --auto-seal seals.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_repair as hr  # noqa: E402


def wt_path(p):
    return str(Path(p))


def main(argv):
    if not argv:
        print("usage: harness-repair-verify.py <worktree> [--require=LIT ...]", file=sys.stderr)
        return 2
    wt = argv[0]
    reqs = [a.split("=", 1)[1] for a in argv[1:] if a.startswith("--require=")]
    sv = hr.survey(wt, reqs)
    if hr.survey_clean(sv):
        # LAST STEP: a repaired harness must re-pass the harness admission gate (strict tier, static
        # checks -- the auto-harness-check half of the verify already measured the contract). Fail-closed:
        # an admission import/runtime error is a NOT_OK, never a pass.
        try:
            import harness_admission as ha
            adm = ha.admit(wt_path(argv[0]), "harness-repair-accept", dynamic=False)
        except Exception as e:
            print("HARNESS_REPAIR_NOT_OK: admission gate could not run (%s: %s)" % (type(e).__name__, e))
            return 1
        if not adm["ok"]:
            print("HARNESS_REPAIR_NOT_OK: repaired harness still refused by the admission gate [%s]" % adm["cause"])
            for d in adm["defects"]:
                print("  admission defect [%s] %s" % (d["cause"], str(d["detail"])[:200]))
            if not adm["defects"]:
                print("  " + str(adm["message"])[:300])
            return 1
        print("HARNESS_REPAIR_OK: relevance gate no longer blocks (verdict relevant); admission gate passed")
        return 0
    print("HARNESS_REPAIR_NOT_OK: %s" % (sv.get("reason") or "relevance gate still blocked"))
    for s in sv.get("survivors") or []:
        print("  SURVIVOR %s:%s %s" % (s.get("file"), s.get("line"), s.get("mutation")))
    for b in sv.get("other_blockers") or []:
        print("  other blocker %s: %s" % (b.get("check") or b.get("id"), str(b.get("message"))[:160]))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
