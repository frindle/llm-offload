#!/usr/bin/env python3
"""CPU-lane tool shipping seam (cardBonuses-v2 964c4a833b82, 2026-10-14).

verify-relevance.py runs on the Unraid runner from the files cpu_stage.host_tools() ships. It imports
mutant_sweep by name (equivalent-mutant exclusion); when that file was not shipped the import failed
SILENTLY (rec["sweep_error"]), every equivalent survivor stayed counted, a correct fixture read
'low', and the finding check (which requires 'relevant') refused to falsify a false reviewer FAIL.

Proves, against the REAL cpu_stage.py / verify-relevance.py / gate-on-complete.py:
  A. every sibling module verify-relevance imports by name (import X / importlib sibling) is shipped;
  B. mutant_sweep.py specifically is in host_tools() and in the eligibility allow-list;
  C. the gate's relevance block carries sweep / sweep_error / equivalent_survivors (not dropped).
"""
import ast, importlib.util, os, re, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIN = Path(sys.argv[sys.argv.index("--bin") + 1]) if "--bin" in sys.argv else HERE
sys.path.insert(0, str(BIN))
FAILS = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + "- " + name + ("" if ok else "  " + str(detail)))
    if not ok:
        FAILS.append(name)


import cpu_stage as cs
tools = cs.host_tools()
shipped = {k[4:] for k in tools}

# A. sibling imports inside verify-relevance.py that exist as files beside it
src = (BIN / "verify-relevance.py").read_text()
tree = ast.parse(src)
mods = set()
for n in ast.walk(tree):
    if isinstance(n, ast.Import):
        mods |= {a.name.split(".")[0] for a in n.names}
    elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
        mods.add(n.module.split(".")[0])
# optional, display-only imports guarded by try/except ImportError that the tool tolerates missing
OPTIONAL = {"dispatch_progress"}
siblings = sorted(m for m in mods if (BIN / (m + ".py")).is_file() and m not in OPTIONAL)
missing = [m for m in siblings if (m + ".py") not in shipped]
check("A every required sibling module verify-relevance imports is shipped to the runner",
      not missing, f"not shipped: {missing} (siblings seen: {siblings})")

# B
check("B mutant_sweep.py is shipped by host_tools()", "mutant_sweep.py" in shipped, sorted(shipped))
check("B' ... and a verify naming it is eligible (allow-list)",
      str(Path.home() / "bin" / "mutant_sweep.py") in cs._shipped_host_paths()
      or (cs.HOST_BIN + "/mutant_sweep.py") in cs._shipped_host_paths())

# C. gate record keeps the sweep outcome
g = (BIN / "gate-on-complete.py").read_text()
check("C gate relevance block carries sweep/sweep_error/sweep_note",
      all(f'"{k}"' in g for k in ("sweep", "sweep_error", "sweep_note")) and 'block["equivalent_survivors"]' in g)

print("CPU_SHIP_OK" if not FAILS else "FAILED: " + ", ".join(FAILS))
sys.exit(1 if FAILS else 0)
