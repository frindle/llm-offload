#!/usr/bin/env python3
"""Score runs/<tag>/<fixture>/run.json against manifest.json (ground truth by construction).

Per fixture, two levels:
  raised     -- any finding the review stage produced (incl. ones the verifier later rejected)
  actionable -- verdict in (None, REAL_DEFECT, UNSURE): what a human would see
Model findings and harness-computed findings (source harness-*) are counted separately,
so a deterministic harness catch is never credited to the model.
A bug is DETECTED when a finding's quote is the defect line / a removed line of the diff,
or its claim/scenario names one of the fixture's keys. Any actionable finding on ok-* is an FP.
usage: score.py <tag> [<tag> ...]   [--detail]
"""
import json
import sys
from pathlib import Path

FX = Path(__file__).resolve().parent
M = json.loads((FX / "manifest.json").read_text())


def removed_lines(diff_path):
    return {l[1:].strip() for l in (FX / diff_path).read_text().splitlines()
            if l.startswith("-") and not l.startswith("---") and l[1:].strip()}


def hits(f, fx):
    q = (f.get("quote") or "").strip()
    blob = " ".join(str(f.get(k, "")) for k in ("claim", "failure_scenario", "concrete_trigger")).lower()
    if fx["defect_line"] and (q == fx["defect_line"].strip() or fx["defect_line"].strip() in q):
        return True
    if q and q in removed_lines(fx["diff"]):
        return True
    return any(k.lower() in blob for k in fx["keys"])


def score(tag, detail=False):
    rows = []
    tot = {"bug": 0, "clean": 0, "det_model": 0, "det_any": 0, "raised_model": 0,
           "fp": 0, "fp_findings": 0, "fp_model": 0, "missing": 0}
    for fx in M["fixtures"]:
        rj = FX / "runs" / tag / fx["id"] / "run.json"
        if not rj.exists():
            tot["missing"] += 1
            rows.append((fx["id"], "MISSING", ""))
            continue
        run = json.loads(rj.read_text())
        fs = run.get("findings", [])
        act = [f for f in fs if f.get("verdict") in (None, "REAL_DEFECT", "UNSURE")]
        mod = [f for f in fs if not str(f.get("source", "")).startswith("harness")]
        mod_act = [f for f in act if not str(f.get("source", "")).startswith("harness")]
        if fx["kind"] == "bug":
            tot["bug"] += 1
            r = any(hits(f, fx) for f in mod)
            d = any(hits(f, fx) for f in mod_act)
            a = any(hits(f, fx) for f in act)
            tot["raised_model"] += r
            tot["det_model"] += d
            tot["det_any"] += a
            st = "DET" if d else ("RAISED-then-rejected" if r else ("harness-only" if a else "MISS"))
        else:
            tot["clean"] += 1
            tot["fp"] += bool(act)
            tot["fp_findings"] += len(act)
            tot["fp_model"] += bool(mod_act)
            st = (f"FP x{len(act)} (model {len(mod_act)})") if act else "clean"
        du = run.get("darkbloom_stats", {})
        rows.append((fx["id"], st, f"calls={run.get('model_calls')} ctoks={du.get('completion_tokens')} "
                                    f"rungs={du.get('schema_rungs')} parsefail={run.get('stats',{}).get('review_parse_fail')}"))
    print(f"== {tag}: model-detected {tot['det_model']}/{tot['bug']} (raised {tot['raised_model']}), "
          f"incl. harness {tot['det_any']}/{tot['bug']}; FP {tot['fp']}/{tot['clean']} fixtures "
          f"({tot['fp_findings']} findings; model-sourced FP {tot['fp_model']}/{tot['clean']})" + (f"; MISSING {tot['missing']}" if tot["missing"] else ""))
    if detail:
        for r in rows:
            print(f"  {r[0]:20s} {r[1]:22s} {r[2]}")
    return tot


if __name__ == "__main__":
    detail = "--detail" in sys.argv
    for t in [a for a in sys.argv[1:] if not a.startswith("--")]:
        score(t, detail)
