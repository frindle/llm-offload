#!/bin/bash
# LIVE smoke for the gate finding check (run AFTER install; read-only w.r.t. real gate records).
#   gate-finding-check-live-smoke.sh enqueue   -> ONE bundle (fcheck-live-smoke), two refute jobs
#                                                on the regate lane: a330 (FP) and 813d (TP)
#   gate-finding-check-live-smoke.sh judge     -> once both are done: execute the MODEL's
#                                                reproducers on the fixture trees, compare with
#                                                ground truth (a330 row0 must NOT confirm;
#                                                813d row0 SHOULD confirm, row1 must NOT).
# Labels are fcheck-smoke-* (NOT secondop-*-fcheck), so the completion hook never routes
# them into a real gate.json. Work dir: ~/.ollama-dispatch/fcheck-live-smoke.
set -euo pipefail
FX="$HOME/bin/test-fixtures-gate-finding-check"
W="$HOME/.ollama-dispatch/fcheck-live-smoke"
MODEL="${GATE_FC_MODEL:-qwen3.6-35b-a3b-vl-mtp-mxfp8}"
HOST="${GATE_FC_HOST:-studio-db}"
mode="${1:-}"
case "$mode" in
enqueue)
  mkdir -p "$W"
  for job in a330fff2d326 813d71882974; do
    d="$W/$job"; mkdir -p "$d"
    python3 - "$FX" "$job" "$d" <<'PY'
import json, sys
sys.path.insert(0, __import__("os").path.expanduser("~/bin"))
import gate_finding_check as g
fx, job, d = sys.argv[1:]
c = f"{fx}/cases/{job}"
p = json.load(open(f"{c}/{job}.gate.json"))
df = [l[6:].strip() for l in open(f"{c}/{job}.diff") if l.startswith("+++ b/")]
pl = g.plan(p, df)
assert pl["action"] == "check", pl
rep = open(f"{c}/regate-report.md").read()
rows = [{"idx": n, "severity": p["issues"][n].get("reviewer_severity") or p["issues"][n]["severity"],
         "file": p["issues"][n]["file"], "line": p["issues"][n]["line"],
         "what": p["issues"][n]["what"], "detail": g.finding_detail(rep, p["issues"][n]["what"])}
        for n in pl["rows"]]
json.dump({"mode": "refute", "rows": rows, "head_tree": f"{c}/head", "diff": f"{c}/{job}.diff",
           "intent": ""}, open(f"{d}/task.json", "w"), indent=1)
PY
    python3 "$HOME/bin/ollama-queue.py" enqueue --model "$MODEL" --host "$HOST" --num-ctx 32768 \
      --cwd "$d" --task-file "$d/task.json" --runner "$HOME/bin/code-review-agent.py" \
      --label "fcheck-smoke-$job" --bundle fcheck-live-smoke --allow-no-verify
  done ;;
judge)
  python3 - "$FX" "$W" <<'PY'
import json, shutil, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path.home() / "bin"))
import gate_finding_check as g
fx, w = Path(sys.argv[1]), Path(sys.argv[2])
truth = {("a330fff2d326", 0): "not-confirmed", ("813d71882974", 0): "confirmed",
         ("813d71882974", 1): "not-confirmed"}
bad = 0
for job in ("a330fff2d326", "813d71882974"):
    rf = w / job / "refute.json"
    if not rf.is_file():
        print(f"{job}: no refute.json yet (job not done?)"); bad += 1; continue
    p = json.load(open(fx / "cases" / job / f"{job}.gate.json"))
    fc = Path(tempfile.mkdtemp(prefix="fc-smoke-"))
    for t in ("base", "head"):
        shutil.copytree(fx / "cases" / job / t, fc / t)
    refute = json.load(open(rf))
    res = g.check_rows(p["issues"], [r["idx"] for r in refute["rows"]], refute, fc)
    for n, r in sorted(res.items()):
        want = truth.get((job, n))
        got = "confirmed" if r["outcome"] == "confirmed" else "not-confirmed"
        flag = "OK " if want == got else ("FALSE-CONFIRM" if got == "confirmed" else "MISSED-TP")
        bad += want != got
        print(f"{flag} {job} row{n}: {r['outcome']} -- {r['why'][:160]}")
        if r.get("reproducer"):
            print("     reproducer kept at", w / job / f"row{n}.py")
            shutil.copy(r["reproducer"], w / job / f"row{n}.py")
    shutil.rmtree(fc, ignore_errors=True)
print("LIVE SMOKE:", "matches ground truth" if not bad else f"{bad} mismatch(es) -- see above")
PY
  ;;
*) echo "usage: $0 enqueue|judge"; exit 2 ;;
esac
