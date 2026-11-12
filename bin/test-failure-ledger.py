#!/usr/bin/env python3
"""failure_ledger: classification, idempotency, SAME_AS, redaction, fail-open, counter, hint.
Builds a synthetic queue-logs/livelogs/state tree in a temp dir (env overrides).
--revert-check: FL_SRC points at a mutated copy; each mutation below must make a case FAIL."""
import importlib.util, json, os, sys, tempfile
from pathlib import Path
SRC = Path(os.environ.get("FL_SRC") or Path(__file__).resolve().parent / "failure_ledger.py")
T = Path(tempfile.mkdtemp(prefix="fl-"))
for k, v in {"FAILURE_LEDGER_QUEUE_LOGS": T/"q", "FAILURE_LEDGER_LIVELOGS": T/"l", "FAILURE_LEDGER_STATE": T/"state.json",
             "FAILURE_LEDGER": T/"failures.jsonl"}.items():
    os.environ[k] = str(v)
(T/"q").mkdir(); (T/"l").mkdir()
spec = importlib.util.spec_from_file_location("fl_t", SRC); fl = importlib.util.module_from_spec(spec); spec.loader.exec_module(fl)
FAILS = []
def chk(n, ok):
    print(("ok  " if ok else "FAIL") + " - " + n)
    if not ok: FAILS.append(n)

def mk(jid, label, calls, reason="nonconvergence", persisted="2026-10-06T10:00:00Z", runlog="", bundle="bun", cap=24, status="failed", cls="model"):
    (T/"q"/f"{jid}.done.json").write_text(json.dumps({"id": jid, "label": label, "bundle": bundle, "status": status, "exit_code": 1,
        "terminal_reason": reason, "failure_class": cls, "persisted_at": persisted, "model": "m", "max_iters": cap}))
    lines = []
    for i, cl in enumerate(calls, 1):
        lines.append(f"[10:00:{i:02d}] [{jid}] \x1b[2m── iteration {i}/{cap} ──\x1b[0m")
        for c in cl:
            lines.append(f"[10:00:{i:02d}] [{jid}] \x1b[1;35m-> calling {c}\x1b[0m")
    (T/"l"/f"{jid}-{label}.livelog").write_text("\n".join(lines) + "\n")
    (T/"q"/f"{jid}-{label}.log").write_text(runlog)

R = lambda p: f"read_file(path='{p}')"
W = lambda p: f"write_file(content='x...', path='{p}')"
V = "run_bash(command='python3 auto-harness-check.py 2>&1 | head -80')"
# 1 read-loop on the harness script, edit at the end never re-verified
mk("aaaa00000001", "auto-author-bun-s4-c1", [[R("auto-harness-check.py")], [R("auto-harness-check.py")], [R("auto-harness-check.py")], [W("refimpl.py")]],
   persisted="2026-10-06T10:00:00Z", runlog="[worker] verify stdout:\n  FAILED CASE: not ok 5 - PUT works\n  FAIL: x\n")
# 2 rewrite loop, verified after last edit
mk("aaaa00000002", "auto-author-bun-s4", [[W("refimpl.py")], [V], [W("refimpl.py")], [V], [W("refimpl.py")], [V]],
   persisted="2026-10-06T11:00:00Z")
# 3 same read-loop again, other slice then same slice
mk("aaaa00000003", "auto-author-bun-s5", [[R("auto-harness-check.py")]]*3 + [[W("refimpl.py")], [V]], persisted="2026-10-06T12:00:00Z")
mk("aaaa00000004", "auto-author-bun-s4-c2", [[R("auto-harness-check.py")]]*3 + [[W("refimpl.py")], [V]], persisted="2026-10-06T13:00:00Z")
mk("aaaa00000011", "auto-author-bun-s4-c3", [[R("auto-harness-check.py")]]*3 + [[W("refimpl.py")], [V]], persisted="2026-10-06T13:30:00Z")
# 5 write_thrash reason
mk("aaaa00000005", "auto-author-bun-s6", [[W("verify.test.ts")], [W("verify.test.ts")]], reason="write_thrash", cls="context", persisted="2026-10-06T14:00:00Z")
# 6 literal cap seen only in the RESULT part; ENOENT in the echoed prompt must NOT trigger env
mk("aaaa00000006", "auto-author-bun-s7", [[W("refimpl.py")], [V]], persisted="2026-10-06T15:00:00Z",
   runlog="[worker] task: ... ENOENT ... plan pins only ...\n[worker] DID NOT CONVERGE\n[worker] verify stdout:\nFAIL: TASK.md `## Must contain` lists 34 literal(s) but the plan pins only 2\n")
# 7 operator stop; 8 a done job must not be recorded; 9 secret in verify tail
mk("aaaa00000007", "auto-author-bun-s8-c2", [[R("a.ts")]], reason="force_stopped", cls="operator", persisted="2026-10-06T16:00:00Z")
mk("aaaa00000008", "auto-author-bun-s9", [[R("a.ts")]], status="done", reason=None, persisted="2026-10-06T16:30:00Z")
mk("aaaa00000009", "auto-author-bun-s9-c1", [[W("a.ts")], [V]], persisted="2026-10-06T17:00:00Z",
   runlog="[worker] verify stdout:\n  token = ghp_abcdefghijklmnopqrstuvwxyz0123456789\n  FAIL: y\n")

n1 = fl.sweep(); n2 = fl.sweep()
rows = {r["job_id"]: r for r in fl.load_rows() if not r.get("event")}
chk("sweep records the 9 terminal-failed jobs, not the done one", n1 == 9 and "aaaa00000008" not in rows)
chk("sweep is idempotent (second sweep appends nothing)", n2 == 0 and len(rows) == 9)
chk("read-loop on harness script (3 reads) classified", rows["aaaa00000001"]["signature"] == "read-loop:auto-harness-check.py")
chk("edit-never-reverified flagged beside it", "edit-never-reverified" in rows["aaaa00000001"]["tags"])
chk("fingerprint carries tool counts + verified flag", rows["aaaa00000001"]["fingerprint"]["tool_counts"]["read_file"] == 3
    and rows["aaaa00000001"]["fingerprint"]["verified_after_last_edit"] is False)
chk("FAILED CASE lines captured", rows["aaaa00000001"]["failed_cases"] == ["PUT works"])
chk("rewrite-loop:refimpl.py (3 write_file of one file)", rows["aaaa00000002"]["signature"] == "rewrite-loop:refimpl.py"
    and rows["aaaa00000002"]["fingerprint"]["rewrites"]["refimpl.py"] == 3)
chk("verified after last edit -> no edit-never-reverified", "edit-never-reverified" not in rows["aaaa00000002"]["tags"])
chk("slice + round parsed from the label", rows["aaaa00000004"]["slice"] == "s4" and rows["aaaa00000004"]["round"] == "c2")
chk("SAME_AS prefers the same slice", rows["aaaa00000004"]["same_as"] == "aaaa00000001" and rows["aaaa00000004"]["same_as_scope"] == "slice")
chk("SAME_AS falls back to another slice", rows["aaaa00000003"]["same_as"] == "aaaa00000001" and rows["aaaa00000003"]["same_as_scope"] == "other-slice")
chk("first of a signature has no SAME_AS", rows["aaaa00000001"]["same_as"] is None)
chk("write_thrash reason -> write-thrash:<file>", rows["aaaa00000005"]["signature"] == "write-thrash:verify.test.ts")
chk("literal-cap detected from the result section", rows["aaaa00000006"]["signature"] == "literal-cap")
chk("ENOENT in the echoed prompt does NOT make it 'env'", "env" not in rows["aaaa00000006"]["tags"])
chk("force_stopped -> operator-stop", rows["aaaa00000007"]["signature"] == "operator-stop")
chk("secrets redacted in the ledger file", "ghp_abcdefghijkl" not in (T/"failures.jsonl").read_text()
    and "[REDACTED]" in (T/"failures.jsonl").read_text())
chk("record_job on an already-recorded job appends nothing", fl.record_job("aaaa00000001") is False
    and sum(1 for r in fl.load_rows() if r.get("job_id") == "aaaa00000001" and not r.get("event")) == 1)
# counter + hint
c = fl.slice_counter("bun", "s4", {"author_jobs_at_retry": 0, "author_attempts": 3}, job_budget=8)
chk("counter: lifetime ids come from done.json (not state) and carry last cause", c["jobs_lifetime"] == 4 and c["last_signature"] == "read-loop:auto-harness-check.py"
    and c["same_as"] == "aaaa00000004" and "same cause as aaaa00000004" in c["text"])
c2 = fl.slice_counter("bun", "s4", {"author_jobs_at_retry": 2}, job_budget=2)
chk("counter: a human-retry window forgives, lifetime does not", c2["jobs_window"] == 2 and c2["jobs_lifetime"] == 4 and c2["at_cap"])
h = fl.signature_hint("bun", "s4")
chk("hint fires when the last two failures of the slice share a signature", "read-loop:auto-harness-check.py" in h and "READING" in h)
chk("hint silent when they differ", fl.signature_hint("bun", "s5") == "")
# fail-open
os.environ["FAILURE_LEDGER"] = "/proc/definitely/not/writable/f.jsonl"
fl.LEDGER = Path("/proc/definitely/not/writable/f.jsonl")
mk("aaaa00000010", "auto-author-bun-s10", [[R("a.ts")]], persisted="2026-10-06T18:00:00Z")
try:
    ok = fl.record_job("aaaa00000010") is False and fl.sweep() >= 0
except Exception as e:
    ok = False
chk("unwritable ledger never raises (fail-open)", ok)
print("ALL PASS" if not FAILS else "FAILED: " + "; ".join(FAILS))
sys.exit(1 if FAILS else 0)
