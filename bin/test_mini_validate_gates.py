"""mini-validate gate helpers. V5: blank and comment-only hunks of a reference fix are skipped (no test can catch them); a hunk with
any code line is still checked. V4: a suite_cmd that is red at baseline is reported (it only proves no NEW failures).
Pure: no git, no runner. Run: python3 test_mini_validate_gates.py"""
import importlib.machinery
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    sp = importlib.util.spec_from_loader(name, ldr)
    m = importlib.util.module_from_spec(sp)
    ldr.exec_module(m)
    return m


V = load("mini_validate_under_test", HERE / "mini-validate")


def ops(a, b):
    a, b = a.splitlines(True), b.splitlines(True)
    return a, b, V.hunks(a, b)


# JS: a reworded doc comment and an added blank line are skipped; the changed code line is not.
base = "/**\n * More than one hit -> ambiguous.\n */\nfunction f(x) {\n  return x;\n}\n"
ref = "/**\n * More than one hit -> broken by tracking, else ambiguous.\n */\n\nfunction f(x) {\n  return x + 1;\n}\n"
a, b, hs = ops(base, ref)
kinds = [V.comment_only(a, b, op) for op in hs]
assert sorted(kinds) == [False, True, True], (hs, kinds)

# A hunk mixing a comment line and a code line is code: V5 still demands a test catch it.
a, b, hs = ops("x = 1\n", "# why\nx = 2\n")
assert len(hs) == 1 and not V.comment_only(a, b, hs[0]), hs

# Python and C-style comments, and a deleted comment, are skipped.
a, b, hs = ops("# old note\ny = 1\n", "y = 1\n")
assert len(hs) == 1 and V.comment_only(a, b, hs[0]), hs
a, b, hs = ops("int z;\n", "/* note */\nint z;\n")
assert len(hs) == 1 and V.comment_only(a, b, hs[0]), hs

# A comment marker after code on the same line does not make it a comment line.
a, b, hs = ops("q = 1\n", "q = 2  # changed\n")
assert len(hs) == 1 and not V.comment_only(a, b, hs[0]), hs

# V4: the runner's baseline lines. Green suite, no suite, and a red typecheck alone are fine; a red suite is named with its first line.
green = "[mini-runner] baseline suite_cmd: rc=0 failure lines=0\n[mini-runner] baseline typecheck_cmd: rc=2 failure lines=205\n" \
        "[mini-runner]   baseline red: app/x.ts(1,2): error TS7006\n"
assert V.suite_red_at_baseline(green) == "", V.suite_red_at_baseline(green)
assert V.suite_red_at_baseline("[mini-runner] try-fix: GREEN\n") == ""
dead = "[mini-runner] baseline suite_cmd: rc=1 failure lines=1\n[mini-runner]   baseline red: Error [ERR_MODULE_NOT_FOUND]: Cannot find module\n" \
       "[mini-runner] baseline typecheck_cmd: rc=2 failure lines=205\n[mini-runner]   baseline red: app/x.ts(1,2): error TS7006\n"
got = V.suite_red_at_baseline(dead)
assert got.startswith("suite_cmd is red at baseline (rc=1, 1 failure line(s), e.g. Error [ERR_MODULE_NOT_FOUND]"), got
# red suite with no failure lines printed: still reported, and never borrows the typecheck's red line
odd = "[mini-runner] baseline suite_cmd: rc=1 failure lines=0\n[mini-runner] baseline typecheck_cmd: rc=2 failure lines=1\n" \
      "[mini-runner]   baseline red: app/x.ts(1,2): error TS7006\n"
got = V.suite_red_at_baseline(odd)
assert got == "suite_cmd is red at baseline (rc=1, 0 failure line(s))", got

print("ok: test_mini_validate_gates")
