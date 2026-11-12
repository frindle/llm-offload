#!/usr/bin/env python3
"""test-queue-slot-decision.py -- the Ollama-lane dual-slot rule (slot_decision) as a canary seam.

WHY (adversarial canary review 2026-10-13): slot_decision is the live guard against two jobs on one
slot / a second weight load on a busy Ollama lane (the 2026-08-28 VRAM collision). Its only tests
lived in `ollama-queue.py --self-test`, which the canary never runs and which is already RED at
baseline (6 stale gate-preempt checks), so deleting its capacity or same-model check left the
canary 239/239 green.

Usage: python3 ~/bin/test-queue-slot-decision.py [--queue PATH]     Prints SLOT_DECISION_OK."""
import argparse, os, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--queue", default=str(Path(__file__).resolve().parent / "ollama-queue.py"))
A = ap.parse_args()
os.environ["HOME"] = tempfile.mkdtemp(prefix="slotdec-")
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
q = SourceFileLoader("q_slotdec", A.queue).load_module()
fails = 0


def chk(name, got, want):
    global fails
    ok = got == want
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f": got {got!r} want {want!r}"))
    fails += 0 if ok else 1


M = "qwen3:14b"
R = {"model": M, "task_kind": "research"}
C = {"model": M, "task_kind": "coding"}
sd = q.slot_decision
chk("free lane -> first claim allowed", sd(M, "coding", [], 1, False)[0], True)
chk("busy single-slot lane -> deny (no second job on one slot)", sd(M, "research", [R], 1, True)[0], False)
chk("2-slot lane already holding 2 -> deny", sd(M, "research", [R, C], 2, True)[0], False)
chk("2-slot lane holding 3 (over-subscribed) -> deny", sd(M, "research", [R, C, R], 2, True)[0], False)
chk("different resident model -> deny (second weight load)", sd(M, "research", [{"model": "other:7b", "task_kind": "research"}], 2, True)[0], False)
chk("unknown resident model -> deny", sd(M, "research", [{"model": None, "task_kind": "research"}], 2, True)[0], False)
chk("two resident models, one matching -> deny", sd(M, "research", [R, {"model": "other:7b", "task_kind": "research"}], 3, True)[0], False)
chk("same model, neither side research -> deny", sd(M, "coding", [C], 2, True)[0], False)
chk("same model + research, KV does not fit -> deny", sd(M, "research", [C], 2, False)[0], False)
chk("same model + research side + free confirmed slot + fits -> allow", sd(M, "research", [C], 2, True)[0], True)
chk("...the research side may be the resident one", sd(M, "coding", [R], 2, True)[0], True)

print("SLOT_DECISION_OK" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
