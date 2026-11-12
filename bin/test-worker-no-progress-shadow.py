#!/usr/bin/env python3
"""Behavioural tests for the SHADOW-MODE no-net-progress telemetry.

WHY THIS FILE EXISTS
--------------------
The worker's two existing anti-thrash guards are both REPETITION-keyed:

  - the read-side early abort needs anti-thrash cache-serves PLUS a worktree
    byte-identical to dispatch START, and
  - write_thrash_abort needs the same (path, content) written 3x.

A run that writes genuinely DIFFERENT bytes every iteration mints a fresh key
each time and arms neither. That is how bg-crypto's s3 ground on until it was
force-stopped (c71f0581d998, 94cfde2bdf95). The uncovered axis is not
self-similarity but FORWARD MOTION: did the tree net any change at all?

WHAT THIS IS DELIBERATELY NOT
-----------------------------
It is not a circuit breaker, and the most important property tested here is
that it CANNOT BECOME ONE by accident. Two reasons it only records:

  1. The motivating failure is already fixed and no longer reproducible. Both
     bg-crypto loops predate the secret-redactor fix that was corrupting
     request_diagnostics; every bg-crypto job run after it completed normally.
  2. The obvious threshold would false-positive on CORRECT behaviour. A model
     that reads and analyses for several iterations before writing has a
     legitimately unchanged tree -- that is what a careful run looks like.

So it gathers the real distribution first. If a threshold is ever wanted it
gets chosen from data rather than guessed, and shipping a live abort on a guess
is exactly the failure this avoids.

Run it directly; it needs no queue, model, network or git.
"""
import importlib.util
import re
import sys
from pathlib import Path

WORKER = Path(__file__).resolve().parent / "ollama-worker.py"

_spec = importlib.util.spec_from_file_location("_ollama_worker_np_test", WORKER)
_w = importlib.util.module_from_spec(_spec)
sys.modules["_ollama_worker_np_test"] = _w
_spec.loader.exec_module(_w)

_n = 0
_fails = []


def check(cond, label):
    global _n
    _n += 1
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        _fails.append(label)


track = _w.no_progress_track


def run(snapshots):
    """Feed a sequence of snapshots through a fresh state and return it."""
    st = {}
    for s in snapshots:
        track(st, s)
    return st


# ---- a stalled run is what this exists to see ----------------------------
st = run(["A", "A", "A", "A"])
check(st["streak"] == 3, "4 identical snapshots -> streak 3 (transitions, not samples)")
check(st["max_streak"] == 3, "...and max_streak agrees while still stalled")
check(st["samples"] == 4, "every non-None snapshot is counted as a sample")

# ---- forward motion resets it -------------------------------------------
st = run(["A", "A", "B"])
check(st["streak"] == 0, "a changed tree resets the streak to 0")

# ---- max_streak is a HIGH-WATER MARK ------------------------------------
# A run that stalled badly and then recovered must not report as if it never
# stalled: the whole point is to learn how deep real stalls get.
st = run(["A", "A", "A", "A", "B", "C"])
check(st["streak"] == 0, "recovery leaves the CURRENT streak at 0")
check(st["max_streak"] == 3, "...but max_streak REMEMBERS the 3-deep stall")
st = run(["A", "A", "B", "B", "B", "B", "C"])
check(st["max_streak"] == 3, "the deepest stall wins, not the most recent one")

# ---- oscillation is NOT a stall by this measure --------------------------
# Write/revert/write/revert nets to nothing across a PAIR but each iteration
# genuinely differs from the one before it. Pinned so the semantics stay
# honest: this measures change-since-last-iteration, and claiming otherwise
# would be the kind of overstated signal that produces false aborts.
st = run(["A", "B", "A", "B", "A", "B"])
check(st["max_streak"] == 0, "A/B oscillation reports NO stall (known limit, by design)")
check(st["samples"] == 6, "...though every iteration is still sampled")

# ---- absence of measurement is not evidence of stalling ------------------
# cwd not a git work tree -> snapshot is None. Counting that as a stall would
# invent a signal from a missing one, the same error the launch-baseline stamp
# refuses to make (a missing key must never read as clean/0).
st = run([None, None, None])
check(st == {}, "None snapshots record NOTHING at all -- not a stall, not a sample")
st = run(["A", None, "A"])
check(st["samples"] == 2, "None is skipped, not counted as a sample")
check(st["streak"] == 1, "...and does not break a genuine stall into two")
st = run([None, "A", "A"])
check(st["max_streak"] == 1, "a leading None does not suppress a real stall")

# ---- degenerate inputs ---------------------------------------------------
check(run([]) == {}, "no iterations -> empty state, no invented keys")
check(run(["A"])["max_streak"] == 0, "a single sample can never be a stall")
check(run(["", ""])["max_streak"] == 1,
      "empty-string snapshot is a VALUE (clean tree), distinct from None")

# ---- the state is returned, and mutated in place -------------------------
_st = {}
check(track(_st, "A") is _st, "returns the same dict it was given (mutates in place)")

# =========================================================================
# THE SHADOW-MODE GUARANTEE
# =========================================================================
# A recorder that can influence the loop is not shadow mode. These assert the
# property structurally, against the production source, because no behavioural
# test of a pure function can show what the CALL SITE does with its result.
#
# Scoped and with needles built at runtime: a source-grep check that can match
# its own search string tests nothing (hit twice earlier tonight, on
# ollama-queue.py's launch re-stamp and gate-on-complete.py's call site).
SRC = WORKER.read_text()

# The telemetry's own identifiers must never appear in a control-flow position.
for kw in ("break", "return", "raise", "continue", "sys.exit"):
    bad = re.findall(
        r"^.*\b" + "no_progress" + r"\b.*$", SRC, re.M)
    offenders = [ln for ln in bad
                 if re.search(r"\b" + kw + r"\b", ln)
                 and not ln.lstrip().startswith("#")]
    check(not offenders,
          f"no line mentioning no_progress also performs `{kw}` "
          f"(offenders: {offenders[:2]})")

_call = "no_progress_track(_no_progress, " + "_git_worktree_snapshot(cwd))"
check(_call in SRC, "the sampler IS wired into the loop (not dead code)")

# It must be swallowed: telemetry that can break a dispatch is worse than none.
_m = re.search(r"if i > NO_PROGRESS_SAMPLE_FLOOR:\n(.*?)\n\n", SRC, re.S)
check(_m is not None, "the sampling block is locatable in the source")
if _m:
    blk = _m.group(1)
    check("try:" in blk and "except Exception:" in blk,
          "the whole sample is inside try/except Exception")
    check("pass" in blk.split("except Exception:")[-1],
          "...and the handler SWALLOWS -- it must not re-raise into the loop")
    check("break" not in blk and "continue" not in blk,
          "the sampling block itself contains no control flow")

# It must be ordered AFTER both early aborts, so it can never pre-empt them.
_i_read = SRC.find('_dispatch_metrics["early_abort"] = ' + '"thrash_zero_diff"')
_i_write = SRC.find('_dispatch_metrics["early_abort"] = ' + '"write_thrash"')
_i_np = SRC.find("if i > NO_PROGRESS_SAMPLE_FLOOR:")
check(_i_read > 0 and _i_write > 0 and _i_np > 0, "control: all three sites found")
check(_i_np > _i_write > _i_read,
      "sampling is ordered AFTER both early-abort blocks (cannot pre-empt them, "
      "and an aborting iteration never pays for the snapshot)")

# And no abort/terminal reason may be keyed on it.
for reason in ("early_abort", "terminal_reason", "pause_reason"):
    lines = [ln for ln in SRC.splitlines()
             if "no_progress" in ln and reason in ln and not ln.lstrip().startswith("#")]
    check(not lines, f"no_progress is never assigned to `{reason}` (offenders: {lines[:2]})")

print()
print(f"{_n - len(_fails)}/{_n} passed")
if _fails:
    print("FAILURES:")
    for f in _fails:
        print("  -", f)
sys.exit(1 if _fails else 0)
