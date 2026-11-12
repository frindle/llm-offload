#!/usr/bin/env python3
"""gate-on-complete.land_ingest_consider: hands a landable standalone dispatch to ollama-land
(fire-and-forget), and nothing else."""
import importlib.machinery, importlib.util, os, sys, tempfile, time
from pathlib import Path
BIN = Path(os.environ.get("LAND_BIN_DIR") or Path(__file__).resolve().parent)
FAILS = []
def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + ((" -- " + str(extra)[:300]) if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)

td = Path(tempfile.mkdtemp(prefix="gate-ingest-"))
fake = td / "bin"; fake.mkdir()
marker = td / "calls.txt"
(fake / "ollama-land").write_text(f"import sys\nopen({str(marker)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n")
os.environ["GATE_BIN"] = str(fake)
os.environ.pop("GATE_TEST_MODE", None); os.environ.pop("DISPATCH_VERIFY_SANDBOX", None)
sys.path.insert(0, str(BIN))
ld = importlib.machinery.SourceFileLoader("gate_oc", str(BIN / "gate-on-complete.py"))
sp = importlib.util.spec_from_loader("gate_oc", ld); G = importlib.util.module_from_spec(sp); ld.exec_module(G)

def calls():
    time.sleep(1.2)
    return marker.read_text().splitlines() if marker.exists() else []

G.land_ingest_consider("job1", {"verdict": "pass"})
check("pass verdict -> ollama-land ingest --job job1", calls() == ["ingest --job job1"], calls())
G.land_ingest_consider("job2", {"verdict": "concerns"})
check("concerns verdict -> ingested too (adjudicated later)", "ingest --job job2" in calls(), calls())
n = len(calls())
G.land_ingest_consider("job3", {"verdict": "fail"})
check("no ingest for fail", len(calls()) == n, calls())
G.land_ingest_consider("job4", {"verdict": "skipped"})
check("no ingest for skipped", len(calls()) == n, calls())
os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
G.land_ingest_consider("job5", {"verdict": "pass"})
check("no ingest from a verify sandbox", len(calls()) == n, calls())
os.environ.pop("DISPATCH_VERIFY_SANDBOX")
(fake / "ollama-land").unlink()
G.land_ingest_consider("job6", {"verdict": "pass"})
check("missing ollama-land is a silent no-op", True)
import re as _re
_src = (Path(G.__file__) if getattr(G, "__file__", None) else Path(sys.argv[0]).parent / "gate-on-complete.py").read_text()
_i = _src.find("runstatus_janitor_consider(a.job_id, payload, gate_json)")
check("the gate's post-gate block actually calls land_ingest_consider (call-site presence)",
      _i > 0 and "land_ingest_consider(a.job_id, payload)" in _src[_i:_i + 400])
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
