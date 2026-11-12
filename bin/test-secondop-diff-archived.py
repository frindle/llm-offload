#!/usr/bin/env python3
"""Guards 2026-10-02 secondop-d4e286641d56 (21763ad589cc, unraid gemma4:12b).

The parent's run-status row was CLEARED 11s after its second opinion was
enqueued; the clear (_archive_run) moved <id>.diff into LOG_DIR/archive/, and the
queued review then crashed: FileNotFoundError traceback, harness-class failure.

Pins: (a) code-review-agent resolves the diff from archive/ when the live copy is
gone; (b) truly missing -> clean exit 0, skipped.json, no traceback, no report.md
(a fake PASS would be false corroboration); (c) the gate merge records a skipped
second opinion as not-run / agreement unknown, verdict unchanged; (d) the merge's
corroboration reads the archived parent diff.

Run: python3 test-secondop-diff-archived.py [--revert-check]
"""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

BIN = Path(__file__).resolve().parent
CRA = Path(os.environ.get("CRA_SRC") or BIN / "code-review-agent.py")
GATE = Path(os.environ.get("GATE_SRC") or BIN / "gate-on-complete.py")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else f" -- got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    ld = importlib.machinery.SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    sys.modules[name] = m
    ld.exec_module(m)
    return m


class SoArgs:
    def __init__(self, jid, label, cwd):
        self.job_id, self.job_label, self.cwd = jid, label, cwd


def main():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        ld = td / "logs"
        (ld / "archive").mkdir(parents=True)
        (ld / "archive" / "p1.diff").write_text("diff --git a/app/x.py b/app/x.py\n+++ b/app/x.py\n@@\n+y = 1\n")
        # (a) archive fallback, via the module function
        cra = None
        try:
            cra = load(CRA, "cra_x")
        except SystemExit:
            pass
        r = getattr(cra, "resolve_diff_path", None)
        check("review agent: archived diff is found",
              r(ld / "p1.diff") if r else None, ld / "archive" / "p1.diff")
        check("review agent: truly-missing diff -> None", r(ld / "nope.diff") if r else "n/a", None)
        # (b) truly missing: clean skip through the real CLI
        out = td / "so"
        out.mkdir()
        p = subprocess.run([sys.executable, str(CRA), "--mode", "review", "--diff", str(ld / "gone.diff"),
                            "--out", str(out), "--model", "none", "--host", "http://127.0.0.1:9"],
                           capture_output=True, text=True, timeout=120)
        check("missing diff: exit 0 (clean skip, not a harness failure)", p.returncode, 0)
        check("missing diff: no traceback", "Traceback" in p.stderr, False)
        check("missing diff: skipped.json written", (out / "skipped.json").is_file(), True)
        check("missing diff: NO report.md (no fake corroboration)", (out / "report.md").exists(), False)
        # (c) gate merge of a skipped second opinion
        g = load(GATE, "gate_so")
        gj = ld / "p1.gate.json"
        gj.write_text(json.dumps({"verdict": "pass", "issues": [], "second_opinion_label": "secondop-p1",
                                  "second_opinion": {"review": "pending", "model": "gemma4:12b-it-q4_K_M",
                                                     "family": "gemma"}}))
        g._terminal_facts = lambda jid: {}
        g.merge_second_opinion(SoArgs("so1", "secondop-p1", str(out)), ld)
        rec = json.loads(gj.read_text())
        check("merge: skipped -> review not-run, verdict unchanged, agreement unknown",
              (rec["second_opinion"]["review"], rec["verdict"],
               rec["second_opinion_agreement"].startswith("unknown"), "second_opinion_disagreement" in rec),
              ("not-run", "pass", True, False))
        # (d) corroboration uses the archived parent diff
        wt = td / "so2"
        wt.mkdir()
        (wt / "report.md").write_text("## VERDICT: PASS\n\n| # | sev | where | what |\n"
                                      "| 1 | high | `lib/other.py:3` | unrelated file |\n")
        gj.write_text(json.dumps({"verdict": "pass", "issues": [], "second_opinion_label": "secondop-p1",
                                  "second_opinion": {"review": "pending", "model": "gemma4:12b-it-q4_K_M",
                                                     "family": "gemma"}}))
        g.merge_second_opinion(SoArgs("so2", "secondop-p1", str(wt)), ld)
        rec = json.loads(gj.read_text())
        check("merge: finding on a file outside the ARCHIVED diff is dropped (corroboration ran)",
              (len(rec["second_opinion"].get("dropped_uncorroborated", [])), rec["second_opinion_agreement"]),
              (1, "agree"))
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


def mutate(path, old, new):
    s = Path(path).read_text()
    assert s.count(old) == 1, f"anchor missing/ambiguous: {old[:70]!r}"
    f = tempfile.NamedTemporaryFile("w", suffix="-mut.py", delete=False)
    f.write(s.replace(old, new))
    f.close()
    return f.name


MUTANTS = [
    ("CRA_SRC", CRA, "no archive fallback",
     "    alt = p.parent / \"archive\" / p.name\n    return alt if alt.is_file() else None",
     "    return None"),
    ("CRA_SRC", CRA, "missing diff crashes again",
     "        _dp = resolve_diff_path(args.diff)\n        if _dp is None:",
     "        _dp = Path(args.diff)\n        if False:"),
    ("GATE_SRC", GATE, "merge ignores skipped.json",
     "    if not report.exists() and skipped.exists():", "    if False:"),
    ("GATE_SRC", GATE, "corroboration ignores archive",
     "        _pd = out_dir / \"archive\" / _pd.name     # parent cleared -> sidecars archived",
     "        pass"),
]


def revert_check():
    bad = 0
    for var, path, name, old, new in MUTANTS:
        mp = mutate(path, old, new)
        r = subprocess.run([sys.executable, __file__], env={**os.environ, var: mp},
                           capture_output=True, text=True, timeout=300)
        os.unlink(mp)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
