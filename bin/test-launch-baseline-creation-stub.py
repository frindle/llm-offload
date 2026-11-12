#!/usr/bin/env python3
"""measure_baseline: a --repo CREATION task's untracked target STUB is not launch
dirt (2026-10-02, idle-test-repo-size aec419e9596e / cc87dbedcdfc false CONCERNS
"dispatch STARTED from a dirty tree (1 uncommitted path)" -> escalation).

The exemption must be narrow: only `??` paths the .dispatch-harness.json declares
(creation target still holding the scaffold stub; authored harness files). Real
dirt -- tracked edits, stray files, a stub clobbered with an implementation, no
manifest, an edit task -- must still count.

QS_SRC env overrides the queue module under test (revert-check: point at the .bak).
"""
import importlib.util, json, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

SRC = Path(os.environ.get("QS_SRC") or Path(__file__).resolve().parent / "ollama-queue.py")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def g(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)


def repo(root, *, tracked_scripts=True, manifest=True, creation=True,
         authored=("TASK.md", "verify.sh", "test_fixture.py")):
    root.mkdir(parents=True)
    g(root, "init", "-q")
    (root / "README.md").write_text("x\n")
    if tracked_scripts:
        (root / "scripts").mkdir()
        (root / "scripts" / "other.sh").write_text("echo\n")
    g(root, "add", "-A")
    g(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    tgt = "scripts/idle_size.py" if tracked_scripts else "newpkg/idle_size.py"
    (root / tgt).parent.mkdir(exist_ok=True)
    (root / tgt).write_text(f'"""Stub for {tgt} -- implement per TASK.md."""\n')
    if manifest:
        (root / ".dispatch-harness.json").write_text(json.dumps(
            {"authored": list(authored), "target": tgt, "creation_task": creation}))
        # the queue's info/exclude handling is the seal's job; mimic a sealed tree
        ex = root / ".git" / "info" / "exclude"
        ex.parent.mkdir(parents=True, exist_ok=True)
        ex.write_text(".dispatch-harness.json\n")
    return root, tgt


def main():
    loader = SourceFileLoader("oq_t", str(SRC))
    spec = importlib.util.spec_from_loader("oq_t", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    mb = m.measure_baseline

    with tempfile.TemporaryDirectory() as td:
        T = Path(td)
        r, _ = repo(T / "1")
        check("creation stub (untracked, tracked dir) is clean", mb(r)["dirty"], 0)

        r, _ = repo(T / "2", tracked_scripts=False)
        check("creation stub in a wholly-untracked dir is clean", mb(r)["dirty"], 0)

        r, t = repo(T / "3")
        (r / t).write_text("def parse_size_gb(s):\n    return float(s)\n")
        check("stub CLOBBERED with an impl still flags", mb(r)["dirty"], 1)

        r, _ = repo(T / "4")
        (r / "README.md").write_text("changed\n")
        check("tracked edit + stub still flags (1)", mb(r)["dirty"], 1)

        r, _ = repo(T / "5")
        (r / "scripts" / "stray.py").write_text("x=1\n")
        check("unrelated untracked file + stub flags (1)", mb(r)["dirty"], 1)

        r, _ = repo(T / "5b", tracked_scripts=False)
        (r / "newpkg" / "stray.py").write_text("x=1\n")
        check("untracked dir holding stub + stray flags", mb(r)["dirty"], 1)

        r, _ = repo(T / "6", manifest=False)
        check("no manifest -> untracked target counts", mb(r)["dirty"], 1)

        r, _ = repo(T / "7", creation=False)
        check("creation_task false -> untracked target counts", mb(r)["dirty"], 1)

        r, _ = repo(T / "8", authored=("TASK.md", "tests/foo.test.ts"))
        (r / "tests").mkdir()
        (r / "tests" / "foo.test.ts").write_text("// t\n")
        check("authored nested harness file (untracked) is clean", mb(r)["dirty"], 0)

        r, t = repo(T / "8b", tracked_scripts=False)
        (r / "newpkg" / "__pycache__").mkdir()
        (r / "newpkg" / "__pycache__" / "idle_size.cpython-314.pyc").write_text("x")
        check("new dir with stub + UNIGNORED __pycache__ is clean", mb(r)["dirty"], 0)

        r, t = repo(T / "9")
        g(r, "add", t)
        g(r, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seal stub")
        (r / t).write_text('"""Stub for x -- implement per TASK.md."""\nX=1\n')
        check("TRACKED target edit is never exempt", mb(r)["dirty"], 1)

    print(f"\n--- {len(FAILS)} failed ---")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
