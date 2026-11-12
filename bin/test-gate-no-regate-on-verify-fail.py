#!/usr/bin/env python3
"""A pre-gate whose only fail is the job's own verify failing must NOT escalate a
re-gate (2026-10-02, real artifacts from bc15112d643a).

Live: auto-author-bfmr-superseded-reservations-v2 failed (output_cap_loop, verify
exit 1). The pre-gate review came back PASS, but the deterministic verify-exit
code_high kept the verdict at fail. The gate still enqueued an authoritative
re-gate --front. No review can overturn a verify failure (merge_review keeps the
verify-exit code_high either way), so the re-gate could only burn a Studio slot.
It also PREEMPTED the bundle's live continuation round (c1 76ee077e738e was paused
by SIGTERM). A review CODE finding without a verify failure must still escalate.

GATE env overrides the file under test. --revert-check mutates the fix."""
import importlib.util, json, os, shutil, subprocess, sys, tempfile, types
from pathlib import Path

HERE = Path(__file__).resolve().parent
GATE = Path(os.environ.get("GATE") or HERE / "gate-on-complete.py")
FX = HERE / "test-fixtures-gate-verify-exit"
sys.path.insert(0, str(HERE))
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def _boom(*a, **k):
    raise RuntimeError("test must not reach a live side effect")


def load_gate():
    spec = importlib.util.spec_from_file_location("gate_vx", str(GATE))
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    sb = Path(tempfile.mkdtemp(prefix="gvx-sandbox-"))
    (sb / "ollama-queue-logs" / "auto-fix").mkdir(parents=True)
    g.BIN, g.COMPLETED_ROOT, g.TEST_MODE, g.TWO_TIER = sb, sb / "Completed", True, True
    g.subprocess = types.SimpleNamespace(run=_boom, Popen=_boom, PIPE=-1, DEVNULL=-3,
                                         TimeoutExpired=Exception, CalledProcessError=Exception)
    return g


def pregate_merge(g, mutate_payload=None, report=None, fx="bc15", parent="bc15112d643a"):
    calls = []
    g._escalate_regate = lambda parent, gj, payload, od: calls.append(parent)
    td = Path(tempfile.mkdtemp())
    try:
        pl = json.loads((FX / f"{fx}.gate.json").read_text())
        pl["issues"] = [i for i in pl.get("issues", []) if i.get("source") != "review"]
        for k in ("gate_authority", "regate", "regate_label", "counts", "review_verdict",
                  "pregate_verdict", "pregate_review_verdict"):
            pl.pop(k, None)
        pl["verdict"] = "pass-pending-review"
        if mutate_payload:
            mutate_payload(pl)
        (td / f"{parent}.gate.json").write_text(json.dumps(pl))
        shutil.copy(FX / f"{fx}.diff", td / f"{parent}.diff")
        rd = td / f"gate-{parent}"
        rd.mkdir()
        (rd / "report.md").write_text(report or (FX / "bc15-review-report.md").read_text())
        a = types.SimpleNamespace(job_label=f"gate-{parent}", cwd=str(rd), job_id="t0", out_dir=str(td))
        try:
            g.merge_review(a, td, prefix="gate-", authoritative=False)
        except RuntimeError as e:
            print("   (merge stopped at a guarded side effect:", e, ")")
        return calls, json.loads((td / f"{parent}.gate.json").read_text())
    finally:
        shutil.rmtree(td, ignore_errors=True)




def main():
    g = load_gate()
    calls, out = pregate_merge(g)
    chk("real bc15112d643a: verify-exit fail + review PASS -> verdict stays fail", out.get("verdict"), "fail")
    chk("...and NO re-gate is escalated", calls, [])
    chk("...and the record says why (not left looking pending)",
        str(out.get("regate", "")).startswith("not-warranted (deterministic verify failure"), True)

    def no_verify_fail(pl):
        pl["issues"] = [i for i in pl.get("issues", []) if i.get("source") != "verify-exit"]
        pl.pop("verify_exit_reported", None)
    calls, out = pregate_merge(g, no_verify_fail, (FX / "defect-report.md").read_text(),
                               fx="s5", parent="96a36dee6870")
    chk("control: a review CODE finding (real s5 report) with no verify failure still escalates",
        (out.get("verdict"), calls), ("fail", ["96a36dee6870"]))
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


MUTANTS = [
    ("verify-exit still escalates", "            and _deterministic_fail:\n", "            and False:\n"),
    ("deterministic predicate never true",
     'any(i.get("source") == "verify-exit" and i.get("severity") == "high"',
     'any(False and i.get("severity") == "high"'),
]


def revert_check():
    src, bad = GATE.read_text(), 0
    for name, old, new in MUTANTS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "GATE": f.name},
                           capture_output=True, text=True, timeout=300)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": {name} -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
