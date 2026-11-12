#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import escalation_ladder as L
f = []
def chk(n, c):
    if not c: f.append(n); print("FAIL", n)
cont = ("continue", 12, {})
skipc = ("skip", "flapping")
d = L.decide(cont, 100, 3600)
chk("resume first", d["rung"] == "resume" and d["extra_iterations"] == 12)
d = L.decide(cont, 100, 3600, admission={"ok": False, "cause": "baseline-not-red"})
chk("harness precedes resume", d["rung"] == "harness" and d["cause"] == "baseline-not-red")
d = L.decide(cont, 100, 3600, admission="ADMISSION_REFUSED: x")
chk("string refusal", d["rung"] == "harness")
d = L.decide(skipc, 100, 3600)
chk("falls to claude", d["rung"] == "claude" and d["action"] == "needs_opus")
chk("skips logged", [s["rung"] for s in d["skipped"]] == ["harness", "resume", "sampling", "second_model", "reslice"])
d = L.decide(skipc, 100, 3600, sampling_mode="live")
chk("sampling live", d["rung"] == "sampling")
d = L.decide(cont, 5000, 3600)
chk("budget spent -> claude", d["rung"] == "claude")
chk("cap never raised", L.effective_cap_s(3600) == 3600 and L.effective_cap_s(99999) == L.HARD_CEILING_S and L.effective_cap_s(None) == 7200)
d = L.decide(cont, 100, 3600, rungs_done=["resume"])
chk("resume not reused", d["rung"] == "claude")
chk("kill switch", not L.enabled({"ESCALATION_LADDER": "0"}) and L.enabled({}))
print("ALL PASS" if not f else f"{len(f)} FAILED"); sys.exit(1 if f else 0)
