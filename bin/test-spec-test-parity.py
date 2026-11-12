#!/usr/bin/env python3
"""SPEC_TEST_PARITY lint (2026-10-11, rt-amazon prose_loop x3): a fixture that asserts EXACT counts / clock values
must have those quantities stated in an 'Exact behaviour contract' section of TASK.md.
  test-spec-test-parity.py                # run
  test-spec-test-parity.py --revert-check # disable each rule in turn; every one must go RED
"""
import importlib.util, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("HG_SRC") or HERE / "dispatch_harness_gates.py")
FAILS = []


def check(name, ok, extra=""):
    print(("ok  " if ok else "FAIL") + ": " + name + ("" if ok else "  " + str(extra)[:300]))
    if not ok:
        FAILS.append(name)


def main():
    spec = importlib.util.spec_from_file_location("hg_t", SRC)
    hg = importlib.util.module_from_spec(spec)
    sys.modules["hg_t"] = hg
    spec.loader.exec_module(hg)
    FX = ("const POLL_ATTEMPTS = 16;\nconst POLL_MS = 500;\n"
          "test('a', async () => {\n  assert.strictEqual(env.detailEvals(), 1 + POLL_ATTEMPTS, 'evals');\n"
          "  assert.strictEqual(env.clock.now, POLL_ATTEMPTS * POLL_MS, 'bounded');\n"
          "  assert.deepStrictEqual(result.tracking, [TBA]);\n  assert.equal(extract(x), '3069');\n"
          "  assert.strictEqual(env.clock.now, 0);\n  assert.ok(env.clock.now >= 3000);\n});\n")
    TASK_NO = "# TASK\n## Required change\nPoll for tracking.\n"
    TASK_OK = TASK_NO + "## Exact behaviour contract\n| POLL_ATTEMPTS | 16 |\n| POLL_MS | 500 |\n"
    with tempfile.TemporaryDirectory() as d:
        wt = Path(d)
        (wt / "verify.test.ts").write_text(FX)
        found, consts = hg.parity_quantities(FX)
        check("quantities: constants found, trivial 0 / data / string / bounds ignored",
              sorted(found) == ["POLL_ATTEMPTS", "POLL_MS"] and consts["POLL_ATTEMPTS"] == "16", (found, consts))
        fs = hg.spec_test_parity_findings(wt, TASK_NO)
        check("no contract section -> one SPEC_TEST_PARITY, says what to add",
              len(fs) == 1 and fs[0]["code"] == hg.SPEC_TEST_PARITY
              and "Add an 'Exact behaviour contract' table listing: POLL_ATTEMPTS, POLL_MS" in fs[0]["message"], fs)
        check("spec_defects() (preflight admission) carries it as SPEC_DEFECT",
              any(f["code"] == hg.SPEC_TEST_PARITY for f in hg.spec_defects(wt, target="a.ts", creation=False, task_text=TASK_NO))
              and hg.SPEC_DEFECT_PREFIX in hg.format_spec_defects(fs))
        check("contract names every constant -> clean", hg.spec_test_parity_findings(wt, TASK_OK) == [])
        check("numeric values (16, 500) instead of names also satisfy",
              hg.spec_test_parity_findings(wt, TASK_NO + "## Expected counts\n16 re-reads, one 500 ms sleep\n") == [])
        part = hg.spec_test_parity_findings(wt, TASK_NO + "## Exact behaviour contract\nPOLL_ATTEMPTS = 16\n")
        check("contract missing one quantity -> flags only that one", len(part) == 1 and "listing: POLL_MS" in part[0]["message"], part)
        check("mention OUTSIDE the contract section does not count",
              len(hg.spec_test_parity_findings(wt, TASK_NO + "POLL_ATTEMPTS POLL_MS 16 500\n")) == 1)
        check("sub-heading inside the section counts; next same-level heading ends it",
              hg.spec_test_parity_findings(wt, TASK_NO + "## Exact contract\n### detail\nPOLL_ATTEMPTS POLL_MS\n## Scope\n") == []
              and len(hg.spec_test_parity_findings(wt, TASK_NO + "## Exact behaviour contract\nx\n## Scope\nPOLL_ATTEMPTS POLL_MS\n")) == 1)
        (wt / "verify.test.ts").write_text(
            "test('x', () => {\n  assert.strictEqual(result.length, 2);\n  assert.strictEqual(result.length, 3);\n"
            "  assert.strictEqual(result.timesEarned, 4);\n  assert.strictEqual(out.length, 0);\n});\n")
        check("bare small data-length asserts never fire (false-positive guard)", hg.spec_test_parity_findings(wt, TASK_NO) == [])
        (wt / "verify.test.ts").write_text("const MAX_N = 8;\ntest('x', () => {\n  assert.strictEqual(calls.length, MAX_N);\n});\n")
        check("a single quantity is below the minimum", hg.spec_test_parity_findings(wt, TASK_NO) == [])
        (wt / "verify.test.ts").write_text("test('x', () => {\n  expect(clock.now).toBe(8000);\n  expect(evals).toBe(17);\n});\n")
        fs = hg.spec_test_parity_findings(wt, TASK_NO)
        check("jest-style expect().toBe on clock/eval literals fires", len(fs) == 1 and "clock.now, 8000" in fs[0]["message"] and "evals, 17" in fs[0]["message"], fs)
        check("8000 satisfied by '8,000' or '8s'; 17 by literal",
              hg.spec_test_parity_findings(wt, TASK_NO + "## Exact behaviour contract\n8s total, 17 evals\n") == [])
        (wt / "verify.test.ts").unlink()
        (wt / "test_fixture.py").write_text("POLL_N = 16\nSTEP_MS = 500\ndef test_a():\n    assert calls == POLL_N\n"
                                            "    assert clock_now == POLL_N * STEP_MS\n")
        check("python fixture asserts parse", len(hg.spec_test_parity_findings(wt, TASK_NO)) == 1)
        hl = importlib.util.spec_from_file_location("hl_t", HERE / "harness_lint.py")
        hlm = importlib.util.module_from_spec(hl)
        hl.loader.exec_module(hlm)
        (wt / "TASK.md").write_text(TASK_NO + "## Must contain\n- `a.py`: `def poll`\n")
        check("harness_lint.run_lint(quick) reports it",
              any(f["code"] == hg.SPEC_TEST_PARITY for f in hlm.run_lint(wt, quick=True, target="a.py")))
    au = (HERE / "ollama-dispatch-auto").read_text()
    check("authoring prompt carries the contract rule (TASK.md section)", "Leave no TODO behind.\n{EXACT_CONTRACT_RULE}" in au)
    check("repair (survivor-kill) prompt carries the matching-contract-line rule",
          "unobservable or the fake is too loose (see below), not a reason to regex it. \"\n    + EXACT_CONTRACT_RULE)" in au
          and "add the matching contract line" in au)
    print("SPEC_TEST_PARITY_OK" if not FAILS else f"FAILED: {FAILS}")
    return 1 if FAILS else 0


MUTATIONS = [
    ("no finding emitted", "        if missing:\n            return [_finding(", "        if False:\n            return [_finding("),
    ("section scoping ignored", "        if not _PARITY_HEADING_RE.search(title):\n            continue\n", ""),
    ("strong-quantity guard dropped", "    return (found if strong else {}), consts", "    return found, consts"),
    ("min-quantities guard dropped", "        if len(found) < SPEC_PARITY_MIN_QUANTITIES:\n            continue\n", ""),
    ("not wired into spec_defects", "    out += spec_test_parity_findings(wt, task)\n", ""),
    ("constants not resolved", "            if all(t in ok for t in re.findall", "            if False and all(t in ok for t in re.findall"),
]


def revert_check():
    bad, src = 0, SRC.read_text()
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing: {name} ({src.count(old)})"
        with tempfile.NamedTemporaryFile("w", suffix="-hg.py", delete=False, dir=str(HERE)) as fh:
            fh.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "HG_SRC": fh.name}, capture_output=True, text=True, timeout=300)
        os.unlink(fh.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
