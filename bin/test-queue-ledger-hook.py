#!/usr/bin/env python3
"""ollama-queue.py's finalize path (_persist_job_completion) must record a terminal-failed job
in the failure ledger, record nothing for a done job, and NEVER let a ledger error escape
(2026-10-06). --revert-check: QUEUE_SRC mutated copy (drops the hook)."""
import importlib.util, json, os, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("QUEUE_SRC") or HERE / "ollama-queue.py")
T = Path(tempfile.mkdtemp(prefix="qlh-")); (T/"q").mkdir(); (T/"l").mkdir()
os.environ.update({"FAILURE_LEDGER_QUEUE_LOGS": str(T/"q"), "FAILURE_LEDGER_LIVELOGS": str(T/"l"),
                   "FAILURE_LEDGER_STATE": str(T/"s.json"), "FAILURE_LEDGER": str(T/"f.jsonl")})
sys.path.insert(0, str(HERE))
ld = SourceFileLoader("q_lh", str(SRC)); m = importlib.util.module_from_spec(importlib.util.spec_from_loader("q_lh", ld))
sys.argv = [str(SRC)]; ld.exec_module(m)
m.LOG_DIR = T/"q"
FAILS = []
def chk(n, ok):
    print(("ok  " if ok else "FAIL") + " - " + n)
    if not ok: FAILS.append(n)
base = {"label": "auto-author-runQ-s1", "bundle": "runQ", "model": "m", "exit_code": 1}
m._persist_job_completion(dict(base, id="qqqq00000001", status="failed", terminal_reason="nonconvergence", failure_class="model"))
m._persist_job_completion(dict(base, id="qqqq00000002", status="done", exit_code=0))
rows = [json.loads(l) for l in (T/"f.jsonl").read_text().splitlines()] if (T/"f.jsonl").exists() else []
chk("failed job recorded at finalize", [r["job_id"] for r in rows] == ["qqqq00000001"] and rows[0]["slice"] == "s1")
import failure_ledger
real = failure_ledger.record_job
failure_ledger.record_job = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    m._persist_job_completion(dict(base, id="qqqq00000003", status="failed", terminal_reason="nonconvergence"))
    ok = (T/"q"/"qqqq00000003.done.json").exists()
except Exception:
    ok = False
failure_ledger.record_job = real
chk("a ledger exception never escapes the finalize path (sidecar still written)", ok)
sys.exit(1 if FAILS else 0)
