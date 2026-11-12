#!/usr/bin/env python3
"""Guards the enqueue-time hole that let two UNWINNABLE jobs launch (2026-09-19).

`ollama-queue.py:_preflight_verify()` refused an unrunnable verify only on exit
126/127. A verify whose SCRIPT is missing does not exit 126/127 -- `python3
missing.py` exits 2 -- so it sailed through and was stamped
"preflight-verify-ok (baseline fails as designed)". The worker then tells the model
those pre-existing failures ARE the bug it was asked to fix, and answers every
task_complete with "NOT ACCEPTED: the verification command still fails" however good
the work is. The model burns its whole budget and the round is recorded as
DID-NOT-CONVERGE -- misread as model incapacity.

Live evidence: jobs 0f3f0e7af69a (auto-refine-esim-global-s1-parse-global-r1) and
acf67b79baf1 (auto-refine-bg-captcha-s3-vision-r1), both `verify =
"python3 auto-harness-check.py"` with that script absent from their cwd, both carrying
the `preflight-verify-ok` stamp. The second had already produced a genuine VERIFY_OK
in its own tree.

The guard must be NARROW: it may only refuse when the missing file is the verify's OWN
script. A CREATION task's fixture imports a target that does not exist yet, so
"No such file" / "Cannot find module" at baseline is BY DESIGN there and refusing it
would break every creation dispatch.

Run: python3 test-queue-preflight-missing-verify-script.py
"""
import importlib.util
import pathlib
import sys
import tempfile

spec = importlib.util.spec_from_file_location(
    "oq", str(pathlib.Path(__file__).resolve().parent / "ollama-queue.py"))
oq = importlib.util.module_from_spec(spec)
sys.modules["oq"] = oq
try:
    spec.loader.exec_module(oq)
except SystemExit:
    pass

fails = []


def check(name, cond, detail=""):
    print(("  ok: " if cond else "  FAIL: ") + name + ("" if cond else " -- " + detail))
    if not cond:
        fails.append(name)


td = pathlib.Path(tempfile.mkdtemp())


def preflight(verify):
    """-> ('refused', msg) | ('allowed', failed_at_baseline)"""
    import contextlib
    import io
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            return "allowed", oq._preflight_verify(verify, td)
    except SystemExit as e:
        return "refused", str(e)


print("=== it REFUSES a verify whose own script is missing ===")
# The exact production case.
kind, msg = preflight("python3 auto-harness-check.py")
check("a missing python verify script is refused", kind == "refused", repr(msg)[:200])
check("the refusal names the missing script",
      kind == "refused" and "auto-harness-check.py" in msg, repr(msg)[:200])
check("the refusal explains the consequence, not just the exit code",
      kind == "refused" and "UNWINNABLE" in msg, repr(msg)[:200])

(td / "verify.test.ts").unlink(missing_ok=True)
kind, msg = preflight("node verify.test.ts")
check("a missing node verify script is refused", kind == "refused", repr(msg)[:200])

print("=== it does NOT refuse a legitimately failing baseline ===")
(td / "f.py").write_text("import sys\nprint('case failed')\nsys.exit(1)")
check("a bug-fix verify that fails at baseline is allowed",
      preflight("python3 f.py") == ("allowed", True))

(td / "ok.py").write_text("print('VERIFY_OK')")
check("a passing verify is allowed and read as passing",
      preflight("python3 ok.py") == ("allowed", False))

# THE FALSE-POSITIVE THAT MATTERS: a creation task. The verify script EXISTS; the
# TARGET it imports does not exist yet -- that is the whole point of a creation task.
(td / "crt.py").write_text(
    "import sys\n"
    "print(\"Error: Cannot find module '../src/newthing.ts'\")\n"
    "print(\"python3: can't open file '/w/src/newthing.py': [Errno 2] \"\n"
    "      'No such file or directory')\n"
    "sys.exit(1)\n")
check("a CREATION task (verify present, target missing) is NOT refused",
      preflight("python3 crt.py") == ("allowed", True),
      "refusing this would break every creation dispatch")

(td / "c.sh").write_text('echo "open data/target.txt: No such file or directory"; exit 1')
check("a verify reporting its own missing DATA file is not refused",
      preflight("bash c.sh") == ("allowed", True))

(td / "d.sh").write_text("cat /does/not/exist; exit 1")
check("a verify that cats a missing path is not refused",
      preflight("bash d.sh") == ("allowed", True))

# The pre-existing 126/127 refusal must still work.
kind, msg = preflight("definitely-not-a-real-command-xyz")
check("the pre-existing not-found refusal still fires",
      kind == "refused" and "127" in msg, repr(msg)[:200])

print("=== baseline-red HONESTY (A/B replay discrepancy 7a34a4de3553): red is not always 'fails as designed' ===")
import contextlib as _cl, io as _io
def full(verify):
    with _cl.redirect_stderr(_io.StringIO()):
        return oq._preflight_verify_full(verify, td)
(td / "meta.py").write_text(
    "import sys\nprint('  FAIL: verify.sh PASSES at baseline -- the fixture cannot fail, so it certifies nothing.')\nsys.exit(1)")
f, why = full("python3 meta.py")
check("a self-check reporting 'verify.sh PASSES at baseline' is red but NOT stamped as designed",
      f is True and why and "HARNESS-DEFECT" in why, repr((f, why)))
(td / "crash.py").write_text("import notamodule_xyz\n")
f, why = full("python3 crash.py")
check("an ImportError crash with no assertion output is red but UNPROVEN", f is True and why and "crash" in why, repr((f, why)))
(td / "assertfail.py").write_text("import sys\nprint('  FAIL: case slugify: expected a-b got a b')\nsys.exit(1)")
check("a genuine assertion failure keeps the 'fails as designed' reading", full("python3 assertfail.py") == (True, None))
check("a bare non-zero exit with no crash evidence keeps the 'fails as designed' reading", full("python3 f.py") == (True, None))
check("a green verify has no suspect reason", full("python3 ok.py") == (False, None))
check("the bool wrapper (_preflight_verify) is unchanged", preflight("python3 meta.py") == ("allowed", True))
check("classifier is PURE and total", oq._classify_baseline_red("") is None and oq._classify_baseline_red(None) is None)

# REVERT-TEST (run by hand, recorded here): delete the `_missing` block from
# _preflight_verify() and the four "refused" checks above FAIL -- the missing-script
# verify is allowed through and reported as "exits 2 ... fails at baseline by design",
# which is exactly the stamp both production job records carry. Drop only the
# `os.path.basename(...) in verify` condition and "a CREATION task ... is NOT refused"
# FAILS instead.

print()
if fails:
    print("QUEUE_PREFLIGHT_FAIL: " + ", ".join(fails))
    sys.exit(1)
print("QUEUE_PREFLIGHT_OK: enqueue refuses a verify whose own script is missing, "
      "without false-refusing a creation task or a by-design failing baseline")
