#!/usr/bin/env python3
"""offload_routing: levers 2 and 5 of the offload plan as POLICY CODE (nothing here touches Darkbloom, the Manager or the queue).

route(kind, ...)        -> where a job of this kind should run and which model, as a decision record
quote_gate(text, srcs)  -> research/review answers: every quoted span must exist verbatim in a source (the quote-must-exist gate)
diagnosis_gate(d)       -> a local diagnosis is accepted only with a repro command that FAILS now and shows the claimed symptom
CLI: decide KIND [--owner-working|--idle] [--paths ...]   one decision (also appended to the dry-run log)
     replay [--specs-dir D] [--models m1,m2] [--estimate] a GPU-time ESTIMATE for replaying every spec through the router; --estimate never runs a model

Flags, all default OFF (decision = keep on Claude):
  OFFLOAD_LOCAL_RESEARCH=1  OFFLOAD_LOCAL_REVIEW=1  OFFLOAD_LOCAL_DIAGNOSE=1     enable a kind
  OFFLOAD_ROUTING=off                                                            global kill switch
  OFFLOAD_ROUTING_DRYRUN=0   (default 1) with 1 a decision is logged to $OFFLOAD_HOME/routing-dryrun.jsonl and 'applied' is false
Model rule (the owner, 2026-10-31): while the owner is working, local work gets the GPU and the resident model is whatever the job needs;
when he is not, the BloomGauge Manager decides, so route() returns model=None, decided_by='bloomgauge-manager' and never picks.
Must-not-offload (money, auth, migrations, gate logic, unknown root cause) always stays on Claude.
"""
import argparse, json, os, re, statistics, sys, time
from pathlib import Path

HOME = Path(os.environ.get("OFFLOAD_HOME") or os.path.expanduser("~/.ollama-dispatch"))
FORBIDDEN = re.compile(r"resell|bfmr|tax|pnl|auth|secret|keychain|token|migrat|mini-validate|mini-swe-runner|mini-submit|mini-spec|canary", re.I)
KIND_FLAG = {"research": "OFFLOAD_LOCAL_RESEARCH", "review": "OFFLOAD_LOCAL_REVIEW", "diagnose": "OFFLOAD_LOCAL_DIAGNOSE"}
# what each kind needs while the owner is working (the resident model follows the job). Names are Darkbloom-served ids, see model_profile.py.
NEEDS = {"coding": "qwen3.6-35b-a3b-vl-mtp-mxfp8", "test_draft": "qwen3.6-35b-a3b-vl-mtp-mxfp8", "research": "qwen3.6-35b-a3b-vl-mtp-mxfp8",
         "review": "qwen3.6-35b-a3b-vl-mtp-mxfp8", "diagnose": "qwen3.6-35b-a3b-vl-mtp-mxfp8"}


def route(kind, owner_working=True, paths=(), env=None):
    e = os.environ if env is None else env
    d = {"kind": kind, "lane": "claude", "model": None, "decided_by": "policy", "applied": False, "why": []}
    if e.get("OFFLOAD_ROUTING", "on") == "off":
        d["why"].append("OFFLOAD_ROUTING=off"); return d
    blob = " ".join(map(str, paths))
    if FORBIDDEN.search(blob):
        d["why"].append("must-not-offload path"); return d
    if kind in KIND_FLAG and e.get(KIND_FLAG[kind]) != "1":
        d["why"].append(f"{KIND_FLAG[kind]} not set"); return d
    if kind not in NEEDS:
        d["why"].append(f"unknown kind {kind}"); return d
    d["lane"] = "darkbloom"
    if owner_working:
        d["model"], d["decided_by"] = NEEDS[kind], "job-needs (the owner working: local has GPU priority)"
    else:
        d["model"], d["decided_by"] = None, "bloomgauge-manager"
    d["applied"] = e.get("OFFLOAD_ROUTING_DRYRUN", "1") == "0"
    d["why"].append("dry-run: decision logged only" if not d["applied"] else "applied")
    return d


def log_decision(d):
    HOME.mkdir(parents=True, exist_ok=True)
    with open(HOME / "routing-dryrun.jsonl", "a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **d}) + "\n")


def _norm(s):
    return re.sub(r"\s+", " ", s).strip()


def quote_gate(text, sources, min_len=12, require_quote=True):
    """Spans in backticks or double quotes of >= min_len chars must occur (whitespace-normalised) in some source. Returns (ok, missing, n_checked)."""
    spans = [m for m in re.findall(r"`([^`\n]{%d,})`|\"([^\"\n]{%d,})\"" % (min_len, min_len), text)]
    spans = [a or b for a, b in spans]
    hay = [_norm(s) for s in sources.values()]
    missing = [q for q in spans if not any(_norm(q) in h for h in hay)]
    ok = not missing and (bool(spans) or not require_quote)
    return ok, missing, len(spans)


def diagnosis_gate(diag, run=None):
    """diag = {claim, repro_cmd, cwd, expect: substring the failing output must show}. Accepted only if the repro FAILS (rc != 0) and its
    output contains `expect`. A diagnosis with no repro, a repro that passes, or one failing for another reason is rejected."""
    import subprocess
    if not diag.get("repro_cmd") or not diag.get("expect"):
        return False, "no failing repro test + expected symptom: not accepted"
    if run is None:
        def run(cmd, cwd):
            p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=600)
            return p.returncode, p.stdout + p.stderr
    rc, out = run(diag["repro_cmd"], diag.get("cwd"))
    if rc == 0:
        return False, "repro passes: it does not reproduce the problem"
    if diag["expect"] not in out:
        return False, "repro fails but not with the claimed symptom"
    return True, "repro fails with the claimed symptom"


def gpu_estimate(models, specs_dir=None, logs=None, n_specs=None):
    """Seconds of GPU a full replay of every spec would take, per model, from history: median active_s of PASSED mini-swe jobs per model
    (fallback: overall median), times the number of specs, times 1 attempt (+ the observed failure rate as retries)."""
    logs = Path(logs or os.path.expanduser("~/bin/ollama-queue-logs"))
    by, allr = {}, []
    for f in logs.glob("*.done.json"):
        try:
            j = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if "mini-swe-runner" not in str(j.get("runner")) or not j.get("active_s"):
            continue
        s = float(j["active_s"]); ok = j.get("runner_verdict") == "PASS"
        by.setdefault(j.get("model"), []).append((s, ok)); allr.append((s, ok))
    sd = Path(specs_dir or os.path.expanduser("~/.ollama-dispatch/mini-runs"))
    n = len([p for p in sd.glob("*/spec.json")]) if sd.is_dir() else 0
    n = n_specs or n
    out = {"specs": n, "models": {}}
    med_all = statistics.median([s for s, ok in allr if ok]) if allr else 0
    for m in models:
        rows = by.get(m) or allr
        passed = [s for s, ok in rows if ok] or [med_all]
        fail_rate = 1 - (sum(1 for _, ok in rows if ok) / len(rows)) if rows else 0
        med = statistics.median(passed)
        out["models"][m] = {"median_pass_s": round(med, 1), "fail_rate": round(fail_rate, 2), "samples": len(by.get(m) or []),
                            "gpu_hours": round(n * med * (1 + fail_rate) / 3600, 1)}
    out["total_gpu_hours"] = round(sum(v["gpu_hours"] for v in out["models"].values()), 1)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decide"); d.add_argument("kind"); d.add_argument("--idle", action="store_true"); d.add_argument("--paths", nargs="*", default=[])
    r = sub.add_parser("replay"); r.add_argument("--specs-dir"); r.add_argument("--models", default="qwen3.6-35b-a3b-vl-mtp-mxfp8"); r.add_argument("--estimate", action="store_true", required=True); r.add_argument("--n-specs", type=int, default=115)
    a = ap.parse_args(argv)
    if a.cmd == "decide":
        dec = route(a.kind, not a.idle, a.paths); log_decision(dec); print(json.dumps(dec, indent=1)); return 0
    print(json.dumps(gpu_estimate(a.models.split(","), a.specs_dir, n_specs=a.n_specs), indent=1)); return 0


if __name__ == "__main__":
    sys.exit(main())
