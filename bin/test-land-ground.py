#!/usr/bin/env python3
"""Behaviour tests for land_ground.py (reviewer-finding grounding + demote-only judge)."""
import json, os, sys, tempfile
from pathlib import Path
BIN = Path(os.environ.get("LAND_BIN_DIR") or Path(__file__).resolve().parent)
sys.path.insert(0, str(BIN))
import land_ground as G

FAILS = []
def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + ((" -- " + str(extra)) if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)

DIFF = """diff --git a/lib/x.ts b/lib/x.ts
--- a/lib/x.ts
+++ b/lib/x.ts
@@ -10,3 +10,4 @@ ctx
 const a = 1;
-const old = hashLink(a);
+const next = hasLink(a);
+export const z = 3;
 tail
"""
FILES = {"lib/x.ts": "\n".join(["l%d" % i for i in range(1, 10)] + ["const a = 1;", "const next = hasLink(a);", "export const z = 3;", "tail"])}
rd = lambda rel: FILES.get(rel)

# the hallucination that motivated this: reviewer says `hashLink` is in the new code
bad = {"severity": "medium", "file": "lib/x.ts", "line": 11, "what": "typo: calls `hashLink` instead of hasLink", "source": "review"}
r = G.ground_findings([bad], DIFF, reader=rd)
bad2 = dict(bad, line=12, what="`hashLink` is undefined here")   # only exists on a REMOVED line
r = G.ground_findings([bad2], DIFF, reader=rd)
check("hashLink hallucination dropped and counted", r["stats"]["dropped"] == 1 and r["stats"]["hallucinated"] == 1, r)
good = {"severity": "medium", "file": "lib/x.ts", "line": 11, "what": "`hasLink(a)` result unused", "source": "review"}
r = G.ground_findings([good], DIFF, reader=rd)
check("grounded finding kept with quote_checked", r["stats"]["kept"] == 1 and r["kept"][0]["quote_checked"] == ["hasLink(a)"], r)
check("legacy review finding without any quote is dropped",
      G.ground_findings([{"severity": "low", "file": "lib/x.ts", "line": 11, "what": "looks wrong", "source": "review"}], DIFF, reader=rd)["stats"]["dropped"] == 1)
check("finding on a file neither in diff nor readable dropped",
      G.ground_findings([{"file": "nope.ts", "line": 1, "quote": "x", "rule": "bug"}], DIFF, reader=rd)["stats"]["dropped"] == 1)
check("rule outside the closed list dropped",
      G.ground_findings([{"file": "lib/x.ts", "line": 11, "quote": "hasLink(a)", "rule": "vibes"}], DIFF, reader=rd)["stats"]["dropped"] == 1)
check("quote far outside the cited line range dropped",
      G.ground_findings([{"file": "lib/x.ts", "line": 1, "quote": "hasLink(a)", "rule": "bug"}], DIFF, reader=rd)["stats"]["dropped"] == 1)
check("a removed line grounds a finding that is ABOUT the removal (rule regression)",
      G.ground_findings([{"file": "lib/x.ts", "line": 11, "quote": "hashLink(a)", "rule": "regression"}], DIFF, reader=rd)["stats"]["kept"] == 1)
check("whitespace-normalised quote matches",
      G.ground_findings([{"file": "lib/x.ts", "line": 12, "quote": "export   const z = 3;", "rule": "bug"}], DIFF, reader=rd)["stats"]["kept"] == 1)
blk = {"file": "lib/x.ts", "line": 11, "quote": "hasLink(a)", "rule": "bug", "severity": "high"}
k = G.ground_findings([blk], DIFF, reader=rd)["kept"][0]
check("blocking finding without repro is demoted to advisory", k["blocking"] is False and "repro" in k["demoted"], k)
k = G.ground_findings([dict(blk, repro="node t.js")], DIFF, reader=rd)["kept"][0]
check("blocking finding WITH repro stays blocking", k["blocking"] is True, k)
mech = {"severity": "high", "file": "", "line": 0, "what": "the task names 7 files", "source": "scope", "category": "input"}
r = G.ground_findings([mech], DIFF, reader=rd)
check("mechanical-source finding passes through ungrounded-check", r["stats"]["kept"] == 1 and r["kept"][0]["grounded"] == "mechanical", r)

# ---- judge: demote-only, unanimous, cached, order-swapped
with tempfile.TemporaryDirectory() as td:
    kept = [dict(blk, repro="x")]
    kept = G.ground_findings(kept, DIFF, reader=rd)["kept"]
    calls = []
    def j_all(f, d, order): calls.append(order); return "demote"
    out = G.judge_findings(kept, DIFF, j_all, cache_dir=td)
    check("unanimous demote -> advisory", out[0]["blocking"] is False and len(calls) == 3, (out, calls))
    check("order is swapped across the 3 votes", set(calls) == {"forward", "reversed"}, calls)
    n = len(calls)
    G.judge_findings(kept, DIFF, j_all, cache_dir=td)
    check("verdict cached: re-run does not call the judge again", len(calls) == n)
    i = [0]
    def j_split(f, d, order): i[0] += 1; return "demote" if i[0] % 3 else "keep"
    with tempfile.TemporaryDirectory() as td2:
        out = G.judge_findings(kept, DIFF, j_split, cache_dir=td2)
        check("split verdict does NOT demote and is flagged", out[0]["blocking"] is True and out[0]["judge"].get("split"), out)
    def j_promote(f, d, order): return "approve"
    with tempfile.TemporaryDirectory() as td3:
        adv = [dict(kept[0], blocking=False)]
        out = G.judge_findings(adv, DIFF, j_promote, cache_dir=td3)
        check("judge can never promote or approve", out[0]["blocking"] is False)
        out = G.judge_findings(kept, DIFF, j_promote, cache_dir=td3)
        check("a non-'demote' answer is a keep", out[0]["blocking"] is True)
    def j_boom(f, d, order): raise RuntimeError("x")
    with tempfile.TemporaryDirectory() as td4:
        check("judge crash == keep (fail closed)", G.judge_findings(kept, DIFF, j_boom, cache_dir=td4)[0]["blocking"] is True)
    check("cache key changes with the diff (new evidence)", G.verdict_key("a", {"x": 1}, "m") != G.verdict_key("b", {"x": 1}, "m"))

# ---- CLI
with tempfile.TemporaryDirectory() as td:
    (Path(td) / "d.patch").write_text(DIFF); (Path(td) / "f.json").write_text(json.dumps([bad2, good]))
    (Path(td) / "lib").mkdir(); (Path(td) / "lib/x.ts").write_text(FILES["lib/x.ts"])
    import subprocess
    p = subprocess.run([sys.executable, str(BIN / "land_ground.py"), "check", "--diff", str(Path(td) / "d.patch"),
                        "--findings", str(Path(td) / "f.json"), "--root", td], capture_output=True, text=True)
    o = json.loads(p.stdout)
    check("CLI drops the hallucination and keeps the real one", o["stats"]["kept"] == 1 and o["stats"]["dropped"] == 1, p.stdout + p.stderr)
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
