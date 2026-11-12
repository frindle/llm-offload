#!/usr/bin/env python3
"""Score bake-off arms from ARTIFACTS, and re-run every verify independently.

Fable's conditions: score from artifacts, re-run the verify per arm, and make
the scorer itself self-discriminating -- "a scorer is a test". So this ships
with --self-check, which feeds it a known-converged and a known-failed arm and
fails unless it separates them. A scorer that cannot tell those apart would
rank models on noise, and nothing else in the pipeline would notice.

Per arm it reports, from real artifacts rather than from the queue's summary:
  status / converged   the metrics row (failures now write one too)
  iterations, cap hit  whether it ran out of room vs finished
  VERIFY RE-RUN        the verify executed again, now, in the job's own cwd
  gate verdict         from <job>.gate.json
  files changed        output-side size measure

The verify re-run is the load-bearing column. "done" has been wrong about a
failing verify repeatedly, so an arm's score must never rest on it.

Usage:
  bakeoff-score.py --arm <job-id> [--arm ...] [--json]
  bakeoff-score.py --self-check --good <job-id> --bad <job-id>
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

BIN = Path.home() / "bin"
LOGS = BIN / "ollama-queue-logs"
METRICS = BIN / "ollama-worker-logs" / "dispatch-metrics.jsonl"


def _load(p, d=None):
    try:
        return json.loads(p.read_text())
    except Exception:
        return d


def metrics_rows():
    rows = []
    try:
        for line in METRICS.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except Exception:
        pass
    return rows


def score(job_id: str, rerun=True) -> dict:
    gate = _load(LOGS / f"{job_id}.gate.json", {}) or {}
    label = gate.get("job_label") or job_id
    cwd, verify = gate.get("job_cwd"), gate.get("job_verify")

    # Match the metrics row by transcript proximity: the row for this job is the
    # one whose task_preview matches, else fall back to nothing rather than to a
    # wrong row -- an unmatched arm must read as unknown, never as a default.
    # Join via the TRANSCRIPT PATH the worker logs, not by looking for the job
    # id inside it -- transcript filenames are timestamps and contain no job id,
    # so the obvious match silently found nothing and every metrics column read
    # as None. The job log is the only place the two identifiers meet.
    row, tpath = None, None
    for lg in sorted(LOGS.glob(f"{job_id}-*.log")):
        try:
            m = re.findall(r"(/\S*ollama-worker-logs/[0-9TZ]+\.json)", lg.read_text())
            if m:
                tpath = m[-1]
        except Exception:
            pass
    if tpath:
        for r in metrics_rows():
            if str(r.get("transcript_path")) == tpath:
                row = r; break

    out = {
        "job": job_id, "label": label,
        "status": gate.get("job_status"), "exit": gate.get("job_exit_code"),
        "gate": gate.get("verdict"),
        "iterations": (row or {}).get("iterations"),
        "hit_iter_cap": (row or {}).get("hit_iter_cap"),
        "files_changed": (row or {}).get("files_changed"),
        "wall_s": (row or {}).get("wall_time_s"),
        "metrics_row": bool(row),
    }

    # THE LOAD-BEARING COLUMN: run the verify again, now.
    if rerun:
        out["verify_rerun"], out["rerun_s"] = rerun_verify(cwd, verify)
    else:
        out["verify_rerun"] = "UNKNOWN (cwd or verify unavailable)"
    return out


def render(rows):
    cols = [("label", 26), ("status", 10), ("gate", 14), ("iterations", 6),
            ("hit_iter_cap", 8), ("verify_rerun", 22), ("files_changed", 6)]
    print("  " + "".join(h.upper()[:w].ljust(w + 2) for h, w in cols))
    for r in rows:
        print("  " + "".join(str(r.get(h, "-"))[:w].ljust(w + 2) for h, w in cols))


def rerun_verify(cwd, verify, timeout=600):
    """Run a verify in a cwd and report PASS/FAIL. ONE definition, so
    --self-check exercises exactly the code that scores real arms rather than a
    copy of it -- a self-check against a duplicate proves nothing about the
    path in use."""
    if not (cwd and verify and Path(cwd).exists()):
        # Unknown is reported as unknown. A missing cwd must not read as a pass.
        return "UNKNOWN (cwd or verify unavailable)", None
    t0 = time.monotonic()
    try:
        rc = subprocess.run(verify, shell=True, cwd=cwd,
                            capture_output=True, timeout=timeout,
                            env={**os.environ, "DISPATCH_VERIFY_SANDBOX": "1"}  # VERIFY-SANDBOX
                            ).returncode
        res = "PASS" if rc == 0 else f"FAIL({rc})"
    except subprocess.TimeoutExpired:
        res = "TIMEOUT"
    return res, round(time.monotonic() - t0, 1)


def self_check_dir(task_dir: str) -> int:
    """Validate the scorer against a task whose BOTH directions bakeoff-roster.py
    has already proven: its solved state must score PASS and its baseline must
    score FAIL.

    This exists because the job-id form needs a known-good and known-bad ARM, and
    early in a bake-off there are none -- the two available job records pointed at
    the SAME cwd, so both scored PASS and the check would have reported separation
    it never demonstrated. A proven-both-ways task dir is a real pair.
    """
    src = Path(task_dir).resolve()
    r = subprocess.run(["git", "-C", str(src), "log", "--format=%H", "--reverse"],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        print(f"FAIL: {src.name} is not a git repo with commits"); return 1
    base_sha = r.stdout.split()[0]

    tmp = Path(tempfile.mkdtemp())
    try:
        solved, baseline = tmp / "solved", tmp / "baseline"
        for d in (solved, baseline):
            shutil.copytree(src, d, symlinks=True,
                            ignore=shutil.ignore_patterns("node_modules", "__pycache__"))
        # reset --hard, not `checkout <sha> -- .`: a file the SOLUTION ADDED is
        # tracked, so checkout cannot delete it and clean skips it, and the
        # solution would survive into the "baseline" run.
        subprocess.run(["git", "-C", str(baseline), "reset", "-q", "--hard", base_sha],
                       capture_output=True)
        subprocess.run(["git", "-C", str(baseline), "clean", "-qfdx"], capture_output=True)

        g, gs = rerun_verify(str(solved), "bash verify.sh")
        b, bs = rerun_verify(str(baseline), "bash verify.sh")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    problems = []
    if g != "PASS":
        problems.append(f"solved state of {src.name} did not score PASS (got {g})")
    if b == "PASS":
        problems.append(f"baseline of {src.name} scored PASS -- the scorer cannot separate them")
    if g == b:
        problems.append("both states scored identically on the load-bearing column")
    print(f"  task:     {src.name}  (baseline {base_sha[:12]})")
    print(f"  solved:   {g}  ({gs}s)")
    print(f"  baseline: {b}  ({bs}s)")
    for pr in problems:
        print("FAIL:", pr)
    print("--- " + ("SCORER DISCRIMINATES" if not problems else f"{len(problems)} problem(s)") + " ---")
    return 1 if problems else 0


def self_check(good: str, bad: str) -> int:
    """A scorer is a test: prove it separates a known-good from a known-bad arm."""
    g, b = score(good), score(bad)
    problems = []
    if g["verify_rerun"] != "PASS":
        problems.append(f"known-GOOD arm {good} did not re-verify PASS (got {g['verify_rerun']})")
    if b["verify_rerun"] == "PASS":
        problems.append(f"known-BAD arm {bad} re-verified PASS -- the scorer cannot separate them")
    if g["verify_rerun"] == b["verify_rerun"]:
        problems.append("both arms scored identically on the load-bearing column")
    print("  good:", json.dumps({k: g[k] for k in ("label", "status", "gate", "verify_rerun")}))
    print("  bad: ", json.dumps({k: b[k] for k in ("label", "status", "gate", "verify_rerun")}))
    for p in problems:
        print("FAIL:", p)
    print("--- " + ("SCORER DISCRIMINATES" if not problems else f"{len(problems)} problem(s)") + " ---")
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", action="append")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-check", action="store_true")
    ap.add_argument("--good"); ap.add_argument("--bad")
    ap.add_argument("--dir", help="self-check against a proven-both-ways task dir")
    ap.add_argument("--no-rerun", action="store_true")
    a = ap.parse_args()

    if a.self_check:
        if a.dir:
            return self_check_dir(a.dir)
        if not (a.good and a.bad):
            print("--self-check needs --good and --bad job ids, or --dir <task-dir>"); return 2
        return self_check(a.good, a.bad)

    if not a.arm:
        print("need --arm <job-id> (repeatable), or --self-check"); return 2
    rows = [score(j, rerun=not a.no_rerun) for j in a.arm]
    if a.json:
        print(json.dumps(rows, indent=1))
    else:
        render(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
