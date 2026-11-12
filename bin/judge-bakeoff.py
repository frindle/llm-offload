#!/usr/bin/env python3
"""Judge bake-off: score candidate pre-gate reviewer models against the
AUTHORITATIVE gate verdict on the corpus of past reviews.

Why this design
---------------
- Corpus: ~/bin/ollama-queue-logs/<job>.gate.json (final verdict) + the
  reviewer INPUT that produced it, still on disk at <job>-review/task.json
  (mode/intent/diff/context) + sibling <job>.diff.
- Label = the AUTHORITATIVE final `verdict`. We score ONLY the 58 "regated"
  cases (regate=="done"): there the Studio-27B authority ruled independently
  of the pre-gate, so the label is not the incumbent judging itself. That is
  also precisely the pre-gate's job: predict what the authority/human decides,
  so it can skip escalation on a real pass and escalate on a real non-pass.
- Judge verdict = gate.py run with the CANDIDATE model, --json, no worktree
  (decidable checks skip -> verdict is review-driven). Reuses gate.py's exact
  findings->verdict mapping, zero drift. Inference goes to --host (Unraid),
  NOT the Studio GPU the authoring arms are using.

Metric: parse-OK rate + agreement, split into
  false-FAIL  (judge non-pass, authority PASS   -> burns GPU on a non-defect)
  false-PASS  (judge PASS,     authority non-pass-> the dangerous miss)
(3-class pass/concerns/fail collapsed to pass vs non-pass for the escalation
decision, which is what the pre-gate actually gates on.)

Usage:
  judge-bakeoff.py --models qwen3:14b gemma4:12b-it-q4_K_M \
      --host http://198.51.100.145:11434 --num-ctx 6144 --limit 24 [--seed 7]
Results -> ~/.ollama-dispatch/bakeoff/judge-results.md (+ judge-raw.jsonl)
"""
import argparse, glob, json, os, random, subprocess, sys, time
from pathlib import Path

LOGS = Path.home() / "bin" / "ollama-queue-logs"
GATE = Path.home() / "bin" / "gate.py"
OUT  = Path.home() / ".ollama-dispatch" / "bakeoff"

def collect(regated_only=True):
    cases = []
    for f in glob.glob(str(LOGS / "*.gate.json")):
        try: d = json.load(open(f))
        except Exception: continue
        v = d.get("verdict")
        if v not in ("pass", "concerns", "fail"):
            continue
        if regated_only and str(d.get("regate")) != "done":
            continue
        job = Path(f).name.replace(".gate.json", "")
        tj  = LOGS / f"{job}-review" / "task.json"
        diff = LOGS / f"{job}.diff"
        if tj.is_file() and diff.is_file():
            cases.append({"job": job, "label": v, "task_file": str(tj),
                          "diff": str(diff), "pregate": d.get("pregate_verdict"),
                          "authority_model": d.get("regate_label") or d.get("review_model")})
    return cases

def is_pass(v): return v == "pass"

def run_judge(model, host, num_ctx, task_file, diff, timeout):
    """Run gate.py with the candidate model; return (verdict|None, secs, err)."""
    t0 = time.time()
    try:
        p = subprocess.run(
            ["python3", str(GATE), "--task-file", task_file, "--diff", diff,
             "--model", model, "--host", host, "--num-ctx", str(num_ctx), "--json"],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, time.time() - t0, "timeout"
    dt = time.time() - t0
    # gate.py --json prints a JSON object; find the last {...} on stdout
    out = p.stdout.strip()
    verdict = None; err = None
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                verdict = json.loads(line).get("verdict")
                break
            except Exception:
                continue
    if verdict is None:
        # maybe whole stdout is one JSON blob
        try: verdict = json.loads(out).get("verdict")
        except Exception: err = (p.stderr.strip()[-200:] or "no-verdict-parsed")
    return verdict, dt, err

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--host", default="http://198.51.100.145:11434")
    ap.add_argument("--num-ctx", type=int, default=6144)
    ap.add_argument("--limit", type=int, default=24, help="cases (stratified sample of the regated gold set)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--all", action="store_true", help="ignore --limit, use every regated case")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    cases = collect(regated_only=True)
    random.seed(a.seed)
    random.shuffle(cases)
    if not a.all:
        cases = cases[:a.limit]
    print(f"gold (regated) cases selected: {len(cases)}  models: {a.models}")
    labdist = {}
    for c in cases: labdist[c["label"]] = labdist.get(c["label"], 0) + 1
    print(f"label distribution: {labdist}")
    if a.dry_run:
        for c in cases[:5]:
            print(f"  {c['job']} label={c['label']} pregate={c['pregate']} auth={c['authority_model']}")
        return

    OUT.mkdir(parents=True, exist_ok=True)
    raw = open(OUT / "judge-raw.jsonl", "w")
    stats = {m: {"n": 0, "parse_ok": 0, "agree": 0, "false_fail": 0,
                 "false_pass": 0, "secs": 0.0} for m in a.models}
    for i, c in enumerate(cases, 1):
        for m in a.models:
            v, dt, err = run_judge(m, a.host, a.num_ctx, c["task_file"], c["diff"], a.timeout)
            s = stats[m]; s["n"] += 1; s["secs"] += dt
            rec = {"job": c["job"], "model": m, "label": c["label"],
                   "judge": v, "secs": round(dt, 1), "err": err}
            raw.write(json.dumps(rec) + "\n"); raw.flush()
            if v in ("pass", "concerns", "fail"):
                s["parse_ok"] += 1
                if is_pass(v) == is_pass(c["label"]):
                    s["agree"] += 1
                elif is_pass(c["label"]) and not is_pass(v):
                    s["false_fail"] += 1
                elif not is_pass(c["label"]) and is_pass(v):
                    s["false_pass"] += 1
            print(f"[{i}/{len(cases)}] {c['job']} {m:28s} judge={str(v):9s} label={c['label']:9s} {dt:.0f}s"
                  + (f"  ERR={err}" if err else ""))
    raw.close()

    # write results.md
    lines = ["# Judge bake-off — candidate pre-gate reviewers vs authoritative verdict", "",
             f"Gold set: {len(cases)} regated cases (Studio-27B authority ruled). "
             f"num-ctx={a.num_ctx}, host={a.host}, seed={a.seed}. {time.strftime('%FT%TZ', time.gmtime())}.",
             f"Label distribution: {labdist}", "",
             "Pass-vs-nonpass (the escalation decision). false-FAIL = judge escalates a real PASS (wasteful); "
             "false-PASS = judge passes a real non-pass (dangerous — but non-terminal: regate+human catch it).", "",
             "| Model | n | parse-OK | agree | false-FAIL | false-PASS | avg s |",
             "|---|---|---|---|---|---|---|"]
    for m in a.models:
        s = stats[m]; n = s["n"] or 1
        lines.append(f"| {m} | {s['n']} | {s['parse_ok']}/{s['n']} | "
                     f"{s['agree']}/{s['parse_ok'] or 1} | {s['false_fail']} | {s['false_pass']} | "
                     f"{s['secs']/n:.0f} |")
    (OUT / "judge-results.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nwrote {OUT/'judge-results.md'} and {OUT/'judge-raw.jsonl'}")

if __name__ == "__main__":
    main()
