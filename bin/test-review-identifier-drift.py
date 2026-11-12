#!/usr/bin/env python3
"""Behavioural test: code-review-agent drops a reviewer finding whose CLAIM rests on
a misspelt identifier that exists nowhere (2026-10-06 gate audit; rt-egift-link-s1
s0: the code had `OrderEgiftLink`, the local reviewer wrote `OrderEgmtLink`).

Drives stage_review end-to-end with a stub model (no network): the drifted finding
must be dropped and counted; a finding naming the REAL identifier, one naming a spec
identifier absent from the code, and one naming an identifier that lives elsewhere
in the changed file must all be kept.

Usage: python3 test-review-identifier-drift.py [path/to/code-review-agent.py]
(pass the .bak to prove it bites)."""
import importlib.machinery, importlib.util, sys
from pathlib import Path
target = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("code-review-agent.py"))
ld = importlib.machinery.SourceFileLoader("cra_drift", str(target))
sp = importlib.util.spec_from_loader("cra_drift", ld)
cra = importlib.util.module_from_spec(sp); ld.exec_module(cra)
ok = True
def check(name, got, want):
    global ok
    good = got == want; ok &= good
    print(("PASS " if good else "FAIL ") + name + ("" if good else f": got {got!r} want {want!r}"))

DIFF = """diff --git a/prisma/schema.prisma b/prisma/schema.prisma
--- a/prisma/schema.prisma
+++ b/prisma/schema.prisma
@@ -1,2 +1,4 @@
 model Order {
+  egiftLinks OrderEgiftLink[]
+}
+model OrderEgiftLink { id Int @id orderId Int @unique }
"""
QUOTE = "model OrderEgiftLink { id Int @id orderId Int @unique }"
SCEN = "an order with two eGift links throws a unique-constraint error on the second insert"
FINDINGS = [
    {"quote": QUOTE, "severity": "high", "claim": "`OrderEgmtLink` is declared with a unique orderId",
     "failure_scenario": SCEN},                                          # drift -> drop
    {"quote": QUOTE, "severity": "high", "claim": "`OrderEgiftLink.orderId` is @unique",
     "failure_scenario": SCEN},                                          # real name -> keep
    {"quote": QUOTE, "severity": "medium", "claim": "spec requires `OrderEgiftLinks` relation name",
     "failure_scenario": SCEN},                                          # spec name -> keep
    {"quote": QUOTE, "severity": "medium", "claim": "`OrderEgiftLog` elsewhere in the file collides",
     "failure_scenario": SCEN},                                          # in full source -> keep
]
class M:
    def chat_json(self, *a, **k):
        return {"findings": [dict(f) for f in FINDINGS]}
hunks = cra.parse_diff(DIFF)
corpus = cra.diff_corpus("\n".join(h.header + "\n" + h.text() for h in hunks))
cra.REF_SOURCES.clear()
cra.REF_SOURCES["prisma/schema.prisma"] = ("model OrderEgiftLog { id Int }", "")
stats = {"quote_rejected": 0, "no_failure_scenario": 0, "review_parse_fail": 0}
out = cra.stage_review(M(), hunks, "add relation `OrderEgiftLinks` on Order", "", corpus, stats)
claims = [f["claim"] for f in out]
check("drifted identifier finding dropped", any("OrderEgmtLink" in c for c in claims), False)
check("drift counted", stats.get("identifier_drift_rejected"), 1)
check("real-identifier finding kept", any(c.startswith("`OrderEgiftLink.orderId`") for c in claims), True)
check("spec-named identifier finding kept", any("OrderEgiftLinks" in c for c in claims), True)
check("identifier from full source kept", any("OrderEgiftLog" in c for c in claims), True)
# ANCHOR regression (2026-10-10): a finding's path:line is computed from the PROMPT rendering
# ("FILE:", "HUNK:", ```diff wrapper lines). Those are not file lines; counting them put
# lib/fmt.ts:5 on a 3-line file and the gate then dropped the real finding as ungrounded.
_d = ("diff --git a/lib/fmt.ts b/lib/fmt.ts\n--- a/lib/fmt.ts\n+++ b/lib/fmt.ts\n@@ -1,2 +1,3 @@\n"
      "-// stub\n-export {};\n+export function formatRange(lo: number, hi: number): string {\n"
      "+  return `[${lo}, ${hi}]`;\n+}\n")
_h = cra.parse_diff(_d)
class M2:
    def chat_json(self, *a, **k):
        return {"findings": [{"quote": "return `[${lo}, ${hi}]`;", "severity": "medium",
                              "claim": "formatRange may mis-handle NaN", "failure_scenario": SCEN}]}
_c = cra.diff_corpus("\n".join(h.header + "\n" + h.text() for h in _h))
_o = cra.stage_review(M2(), _h, "add formatRange", "", _c,
                      {"quote_rejected": 0, "no_failure_scenario": 0, "review_parse_fail": 0})
check("review finding anchors to the real file line (2), not prompt-wrapper-shifted",
      cra._anchor(_o[0]) if _o else None, "lib/fmt.ts:2")
print("ALL PASS" if ok else "SOME FAILED"); sys.exit(0 if ok else 1)
