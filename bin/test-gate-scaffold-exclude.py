#!/usr/bin/env python3
"""Regression test for Bug #7 (2026-09-18): scaffold/harness files must NOT leak
into the gate diff of an auto-author job.

Job aw-airport-groups (auto-author-...-s1-is-group) came back "6 changed files" for
a single-file creation slice -- 5 of the 6 were scaffold/harness files
(check_literals.py, test_fixture.py, auto-harness-check.py, AUTO-TASK.md,
.dispatch-harness.json) that leaked into the diff because get_diff's untracked
inclusion loop used a SHORT hand-copied exclusion list instead of the canonical
_SCAFFOLD_BASENAMES. On an author job the digest-match escape hatch cannot help
(the model is supposed to rewrite the harness; no digests are pinned on first
author), so those files rode into the diff and the review spoke to non-model code.

This builds a real temp git repo with untracked scaffold files + a real target and
asserts get_diff EXCLUDES every scaffold basename and KEEPS the target.

Red-on-revert: restore the short inline set (drop the _SCAFFOLD_BASENAMES union)
and the check_literals.py / test_fixture.py / auto-harness-check.py / AUTO-TASK.md /
.dispatch-harness.json assertions fail (they reappear in the diff).

Run: python3 test-gate-scaffold-exclude.py
"""
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

GATE = Path(__file__).resolve().parent / "gate-on-complete.py"


def _load():
    spec = importlib.util.spec_from_file_location("goc_test", GATE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True,
                   capture_output=True, text=True)


failures = []


def ok(name, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")
    if not cond:
        failures.append(name)


def main():
    m = _load()
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")
        (repo / "README.md").write_text("base\n")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-qm", "base")

        # Untracked scaffold/harness files (must be excluded).
        scaffold_files = ["check_literals.py", "test_fixture.py",
                          "auto-harness-check.py", "AUTO-TASK.md",
                          ".dispatch-harness.json", "TASK.md", "verify.sh",
                          "refimpl.py"]
        for f in scaffold_files:
            (repo / f).write_text(f"# scaffold {f}\ncontent\n")

        # The real target (must be KEPT in the diff).
        (repo / "src").mkdir()
        (repo / "src" / "airport_groups.py").write_text(
            "def is_group(x):\n    return x in GROUPS\n")

        out = repo / "diff.out"
        excluded = []
        diff_path, how = m.get_diff(repo, out, scaffold=None, excluded=excluded)

        ok("target is in the diff", diff_path is not None
           and "src/airport_groups.py" in diff_path.read_text())

        text = diff_path.read_text() if diff_path else ""
        for f in ["check_literals.py", "test_fixture.py", "auto-harness-check.py",
                  "AUTO-TASK.md", ".dispatch-harness.json"]:
            ok(f"scaffold file excluded from diff: {f}",
               f in excluded and f not in text)

        # These were already excluded by the legacy list -- assert they still are.
        for f in ["TASK.md", "verify.sh", "refimpl.py"]:
            ok(f"legacy-excluded still excluded: {f}", f in excluded)

    if failures:
        print(f"\n{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("\nall gate-scaffold-exclude tests passed")


if __name__ == "__main__":
    main()
