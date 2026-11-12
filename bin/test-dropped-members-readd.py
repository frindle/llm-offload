#!/usr/bin/env python3
"""Regression: dropped_members() must not flag a literal that reappears on the
added side of the same hunk. See VehicleCard false positive (2b31109a8e8b):
'TAP DIAL TO SET' present verbatim on the added line was reported "dropped"
because _COLLECTION could not parse the nested-ternary collection that now
holds it, so it never entered added_all. A genuine deletion must still fire."""
import importlib.util, sys, os
os.chdir(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("cra", "code-review-agent.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
Hunk, dropped_members = m.Hunk, m.dropped_members

def hunk(lines): return Hunk("f.tsx", "@@", lines)

fails = []

# 1. FALSE POSITIVE case: removed+re-added literal inside a now-nested ternary.
fp = hunk([
 "-          {v.ctrl === 'full' ? 'CHARGE LIMIT' : 'CHARGE LIMIT · VIA SCHEDULE'} · {canEditLimit ? 'TAP DIAL TO SET' : 'SET VIA RIVIAN APP'} · SOURCE {v.apiLabel}",
 "+          {v.ctrl === 'full' ? 'CHARGE LIMIT' : 'CHARGE LIMIT · VIA SCHEDULE'} · {canEditLimit ? 'TAP DIAL TO SET' : (v.ctrl === 'full' ? 'SET IN TESLA APP' : 'SET VIA RIVIAN APP')} · SOURCE {v.apiLabel}",
])
res = dropped_members(fp)
gone = [g for _, gs in res for g in gs]
if "TAP DIAL TO SET" in gone:
    fails.append(f"FP: 'TAP DIAL TO SET' reported dropped though present on added side. got={res}")
else:
    print("PASS[1] re-added literal not flagged as dropped")

# 2. TRUE DELETION must still fire: 'refunded' truly gone from the added side.
td = hunk([
 "-    status in ('cancelled', 'refunded')",
 "+    status in ('cancelled',)",
])
res2 = dropped_members(td)
gone2 = [g for _, gs in res2 for g in gs]
if "refunded" not in gone2:
    fails.append(f"TD: genuine deletion of 'refunded' NOT detected. got={res2}")
else:
    print("PASS[2] genuine deletion still detected")

# 3. Reappears elsewhere on added side (not the collection) -> not dropped.
re_case = hunk([
 "-    keys = ('alpha', 'beta')",
 "+    keys = ('alpha',)  // 'beta' handled by handleBeta('beta')",
])
res3 = dropped_members(re_case)
gone3 = [g for _, gs in res3 for g in gs]
if "beta" in gone3:
    fails.append(f"RE: 'beta' flagged dropped though it reappears on added side. got={res3}")
else:
    print("PASS[3] literal reappearing elsewhere on added side not flagged")

if fails:
    print("\nFAILURES:"); [print("  -", f) for f in fails]; sys.exit(1)
print("\nALL PASS"); sys.exit(0)
