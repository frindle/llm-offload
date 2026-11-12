#!/usr/bin/env python3
"""Guards the fix of 2026-10-02: the gate auto-fix path requeued a fix round for a
DIAGNOSIS-only job (plex-automation pa-grab-dedupe-diagnosis 37154c38fc6f ->
4ed5b4908033 [auto-fix r1], thrashed, escalated). A diagnosis is enqueued
task_kind=coding with a DIAGNOSIS.md deliverable, so the research-only guard missed
it. autofix_consider must never classify/requeue an investigation job.

Run: python3 test-gate-no-autofix-investigation.py [--revert-check]
"""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

GATE = Path(os.environ.get("GATE_SRC", Path(__file__).resolve().parent / "gate-on-complete.py"))
FAILS = []

DIAG_TASK = """# DIAGNOSIS TASK -- test_grab_dedupe.py fails on main (investigate, do not fix)
## Symptom (observed)
AssertionError: []
## What to do
DIAGNOSE from the evidence ... Write your findings to DIAGNOSIS.md with a Root cause section.
"""
DIAG_VERIFY = "bash /Users/user/.ollama-dispatch/pa-diag-verify.sh"
CODE_TASK = """# TASK: bfmr-superseded-reservations-v3
## Required change
Add an exported PURE function supersededRowIds to lib/bfmrVanished.ts. Run `bash verify.sh`.
"""


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else f" -- got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


def load():
    spec = importlib.util.spec_from_loader("goc_inv", importlib.machinery.SourceFileLoader("goc_inv", str(GATE)))
    m = importlib.util.module_from_spec(spec)
    sys.modules["goc_inv"] = m
    spec.loader.exec_module(m)
    return m


def run_consider(m, td, task_text, verify, label):
    tf = Path(td) / f"{label}.md"
    tf.write_text(task_text)
    fields = {"task_file": str(tf), "verify": verify, "task_kind": "coding", "label": label}
    m._job_field = lambda jid, k: fields.get(k)
    m.job_facts = lambda jid: {"job_verify": verify, "job_label": label}
    called = []

    def fake_classify(payload):
        called.append(1)
        raise RuntimeError("stop after classify (control path reached)")
    m.autofix_classify = fake_classify
    gj = Path(td) / f"{label}.gate.json"
    payload = {"job_label": label, "verdict": "concerns"}
    gj.write_text(json.dumps(payload))
    m.autofix_consider("j-" + label, payload, gj, Path(td))
    return payload, called


def main():
    m = load()
    check("detector: diagnosis task + DIAGNOSIS verify", m.is_investigation_task(DIAG_TASK, DIAG_VERIFY), True)
    check("detector: DIAGNOSIS.md named only in verify",
          m.is_investigation_task("Explain why test_x fails on main.", "test -s DIAGNOSIS.md"), True)
    check("detector: research kind", m.is_investigation_task("anything", "", "research"), True)
    check("detector: coding task is NOT investigation", m.is_investigation_task(CODE_TASK, "bash verify.sh"), False)
    check("detector: 'diagnostic message' string alone is NOT investigation",
          m.is_investigation_task("change the diagnostic message text in app.py", "bash verify.sh"), False)
    with tempfile.TemporaryDirectory() as td:
        payload, called = run_consider(m, td, DIAG_TASK, DIAG_VERIFY, "pa-grab-dedupe-diagnosis")
        check("diagnosis job: classifier never reached (no fix round built)", called, [])
        check("diagnosis job: auto_fix_action none", payload.get("auto_fix_action"), "none")
        check("diagnosis job: class investigation-no-code", payload.get("auto_fix_class"), "investigation-no-code")
        payload2, called2 = run_consider(m, td, CODE_TASK, "bash verify.sh", "bfmr-superseded-reservations-v3")
        check("coding job (control): classifier IS reached", called2, [1])
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("guard removed", "        if _job_is_investigation(job_id, payload):\n", "        if False:\n"),
    ("DIAGNOSIS.md signal removed", '    if "DIAGNOSIS.md" in hay or "DIAGNOSIS.md" in v:\n        return True\n', ""),
]


def revert_check():
    src = GATE.read_text()
    bad = 0
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"mutation anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-goc.py", delete=False) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "GATE_SRC": f.name},
                           capture_output=True, text=True)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
