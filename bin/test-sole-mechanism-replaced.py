#!/usr/bin/env python3
"""Regression: a model finding claiming a removed line was the SOLE/ONLY
mechanism (or "the deleted line was the ...") must NOT survive grounding when
that line's mechanism is REPLACED on the added side of the same hunk. But a
GENUINE deletion (removed, nothing similar re-added) MUST still be flagged.

See resell BG-credited FP (cce40aa3eeba): the review model raised two HIGH
findings anchored to `-if (isInBalance && !x.bgCredited) creditedOrderIds.add(x.id)`,
each immediately replaced by `+if (isInBalance) creditTrackingForOrder(x.id, ...)`.
The removed line is not re-added verbatim, so the older moved-not-deleted check
(verbatim, removal-pass only) missed it and a review-source finding was never
checked. Crediting still happens via the new creditTrackingForOrder +
isOrderFullyCredited path, so the "sole mechanism, now gone" premise is false."""
import importlib.util, sys, os
os.chdir(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("cra", "code-review-agent.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
Hunk = m.Hunk
overturns = m.overturns_sole_mechanism_claim
removed_replaced = m.removed_line_was_replaced

def hunk(lines): return Hunk("lib/bgSync.ts", "@@", lines)

fails = []
def check(name, cond):
    if cond: print(f"PASS[{name}]")
    else: fails.append(name)

# The real cce40aa3eeba remove-and-replace hunk (3 sibling pairs).
replace_hunk = hunk([
 " bgMatchedOrderIds.add(orderNumMatch.id);",
 "-            if (isInBalance && !orderNumMatch.bgCredited) creditedOrderIds.add(orderNumMatch.id);",
 "+            if (isInBalance) creditTrackingForOrder(orderNumMatch.id, trackingId || null);",
 " bgMatchedOrderIds.add(match.id);",
 "-            if (isInBalance && !match.bgCredited) creditedOrderIds.add(match.id);",
 "+            if (isInBalance) creditTrackingForOrder(match.id, trackingId || null);",
 " bgMatchedOrderIds.add(o.id);",
 "-              if (isInBalance && !o.bgCredited) creditedOrderIds.add(o.id);",
 "+              if (isInBalance) creditTrackingForOrder(o.id, trackingId || null);",
])

# ---- (a) FALSE POSITIVE direction: the two REAL gate-JSON claims -------------
gate1 = {"source": "review",
         "quote": "if (isInBalance && !orderNumMatch.bgCredited) creditedOrderIds.add(orderNumMatch.id);",
         "claim": "The deleted line was the sole mechanism that added an order ID to `creditedOrderIds` during the single-match path."}
gate2 = {"source": "review",
         "quote": "if (isInBalance && !o.bgCredited) creditedOrderIds.add(o.id);",
         "claim": "The deleted line was the sole mechanism that marked an order as BG-credited (`creditedOrderIds.add(o.id)`)."}
check("gate1-suppressed", overturns(gate1, [replace_hunk]) is True)
check("gate2-suppressed", overturns(gate2, [replace_hunk]) is True)

# removal-pass phrasing variant ("This line has been deleted, but the
# replacement logic ...") with an exclusivity marker must also be caught.
rp = {"source": "removal-pass",
      "quote": "if (isInBalance && !match.bgCredited) creditedOrderIds.add(match.id);",
      "claim": ("This line was the only place an order was marked BG-credited. "
                "It has been deleted, but the replacement does not ...")}
check("removalpass-suppressed", overturns(rp, [replace_hunk]) is True)

# ---- (b) TRUE DELETION direction: must STILL be flagged ----------------------
# umask 077 removed; only an unrelated chmod added -> no structural replacement.
del_hunk = hunk([
 "-umask 077",
 ' echo "$SECRET" > "$DOC_FILE"',
 '+chmod 600 "$DOC_FILE"',
])
genuine = {"source": "removal-pass", "quote": "umask 077",
           "claim": "`umask 077` was the sole mechanism restricting the file's permissions; it has been silently removed."}
check("genuine-deletion-kept", overturns(genuine, [del_hunk]) is False)
check("genuine-not-replaced", removed_replaced("umask 077", [del_hunk]) is False)

# A fully deleted guard (no similar add) with a sole-mechanism claim: kept.
guard_hunk = hunk([
 "-  if (!token) return;",
 "   const result = doWork(token);",
])
guard = {"source": "review", "quote": "if (!token) return;",
         "claim": "This was the only guard preventing doWork from running on a null token."}
check("genuine-guard-kept", overturns(guard, [guard_hunk]) is False)

# ---- (c) SCOPE guards: don't over-suppress ----------------------------------
# Claim that merely doubts the NEW impl (no exclusivity marker) -> NOT suppressed.
doubt = {"source": "review",
         "quote": "if (creditedOrderIds.has(order.id) && !order.bgCredited) {",
         "claim": ("The condition checks if the order was uncredited, but this "
                   "logic is removed and replaced with a different implementation "
                   "that may not correctly track when all shipments are credited.")}
check("doubt-new-impl-kept", overturns(doubt, [replace_hunk]) is False)

# Harness findings are deterministic facts -> never suppressed by this filter.
harness = {"source": "harness-dropped-member",
           "quote": "if (isInBalance && !o.bgCredited) creditedOrderIds.add(o.id);",
           "claim": "'refunded' was the sole value silently dropped from the set."}
check("harness-untouched", overturns(harness, [replace_hunk]) is False)

# A sole-mechanism claim about ADDED code (not a removed line) -> not our class.
added_only = {"source": "review", "quote": "creditTrackingForOrder(o.id, trackingId || null);",
              "claim": "This is the only mechanism now, and it is wrong."}
check("added-line-kept", overturns(added_only, [replace_hunk]) is False)

if fails:
    print("\nFAILURES:"); [print("  -", f) for f in fails]; sys.exit(1)
print("\nALL PASS"); sys.exit(0)
