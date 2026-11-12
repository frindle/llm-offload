#!/usr/bin/env python3
"""test-noreread.py -- static seam `noreread` (pipeline-hardening-design section 1).

The C10 bug class: a chooser decides from one read of a Darkbloom vendor file and a
consumer re-reads it moments later, while the provider restart has rewritten it. The
design makes this STRUCTURALLY detectable: the vendor-file names may appear as string
literals only in lane_plan.py and vendor_contract.py (offline shape checks need the raw
keys; vendor_contract drops the api_key value on read) plus the existing bloom_control.py
mutator allowlist entry. conformance_log.py never reads vendor files.

Until the Wave 2-4 migration moves the queue/worker/darkbloom_chat/review-agent off the
raw files, the current literals are a RATCHET: per file and per name the count may only go
DOWN. Wave 4 sets BASELINE to {} (every count 0) when the stickies are deleted.
Docstrings and comments are not reads and are ignored; any other string constant
(including the literal parts of an f-string) counts.

  python3 test-noreread.py [--bin DIR]      -> prints NOREREAD_OK
"""
from __future__ import annotations

import ast
import sys
import tempfile
from pathlib import Path

NAMES = ("local.json", "provider.toml", "loaded-models.json", "daemon-state.json")
SCANNED = ("ollama-queue.py", "ollama-worker.py", "darkbloom_chat.py",
           "code-review-agent.py", "gate-on-complete.py")
ALLOWED = ("lane_plan.py", "vendor_contract.py", "bloom_control.py")   # may hold the literals
# Ratchet baseline measured 2026-10-14 (before the plan is wired). Counts only go down.
BASELINE = {
    "ollama-queue.py": {"local.json": 2, "provider.toml": 2, "loaded-models.json": 2},
    "ollama-worker.py": {"local.json": 6, "provider.toml": 1, "loaded-models.json": 1},
    "darkbloom_chat.py": {"local.json": 2, "provider.toml": 1, "loaded-models.json": 1},
    "code-review-agent.py": {},
    "gate-on-complete.py": {"local.json": 1},
}

FAILS: list[str] = []


def check(name: str, ok: bool, note: str = "") -> None:
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  -- {note}"))
    if not ok:
        FAILS.append(name)


def literal_counts(src: str) -> dict:
    """{name: count} of non-docstring string constants containing a vendor-file name."""
    tree = ast.parse(src)
    doc = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            b = n.body
            if b and isinstance(b[0], ast.Expr) and isinstance(getattr(b[0], "value", None), ast.Constant) \
                    and isinstance(b[0].value.value, str):
                doc.add(id(b[0].value))
    out: dict = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc:
            for k in NAMES:
                if k in n.value:
                    out[k] = out.get(k, 0) + 1
    return out


def violations(bin_dir: Path, baseline=None, scanned=None) -> list[str]:
    baseline = BASELINE if baseline is None else baseline
    out = []
    for f in (scanned or SCANNED):
        p = bin_dir / f
        if not p.is_file():
            out.append(f"{f}: scanned file missing (a rename must not make the seam vacuous)")
            continue
        got = literal_counts(p.read_text())
        for k, n in sorted(got.items()):
            if n > baseline.get(f, {}).get(k, 0):
                out.append(f"{f}: {k!r} literal x{n} > allowed {baseline.get(f, {}).get(k, 0)} "
                           "(read vendor files only in lane_plan.py / vendor_contract.py)")
    return out


def self_tests() -> None:
    # the scanner itself, on fixtures (the real tree is checked below)
    c = literal_counts('def f():\n    return open("/x/local.json").read()\n')
    check("scan.plain_literal_counted", c == {"local.json": 1}, str(c))
    c = literal_counts('def f(h):\n    return f"{h}/provider.toml"\n')
    check("scan.fstring_part_counted", c == {"provider.toml": 1}, str(c))
    c = literal_counts('"""reads local.json"""\ndef f():\n    """daemon-state.json"""\n    # loaded-models.json\n    return 1\n')
    check("scan.docstring_and_comment_ignored", c == {}, str(c))
    c = literal_counts('P = "a/local.json"\nQ = "b/local.json"\n')
    check("scan.counts_each", c == {"local.json": 2}, str(c))
    with tempfile.TemporaryDirectory() as d:
        dd = Path(d)
        (dd / "a.py").write_text('X = "daemon-state.json"\n')
        v = violations(dd, baseline={"a.py": {}}, scanned=("a.py",))
        check("scan.new_literal_is_violation", len(v) == 1, str(v))
        v = violations(dd, baseline={"a.py": {"daemon-state.json": 1}}, scanned=("a.py",))
        check("scan.baseline_literal_allowed", v == [], str(v))
        v = violations(dd, baseline={}, scanned=("missing.py",))
        check("scan.missing_scanned_file_is_violation", len(v) == 1 and "missing" in v[0], str(v))


def main() -> int:
    bin_dir = Path(__file__).resolve().parent
    if "--bin" in sys.argv:
        bin_dir = Path(sys.argv[sys.argv.index("--bin") + 1]).resolve()
    self_tests()
    check("allowlist.disjoint_from_scanned", not set(ALLOWED) & set(SCANNED))
    check("allowlist.files_exist_in_tree", all((bin_dir / f).is_file() for f in ("lane_plan.py", "vendor_contract.py")),
          "lane_plan.py / vendor_contract.py missing")
    v = violations(bin_dir)
    check("tree.no_new_vendor_file_literals", not v, "; ".join(v))
    print("ALL PASS" if not FAILS else f"FAILED: {FAILS}")
    if not FAILS:
        print("NOREREAD_OK")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
