#!/usr/bin/env python3
"""gate_identity.py -- WHAT a gate verdict judged, and whether it still holds.

WHY (2026-10-06 gate audit, the owner: "get the gate issues worked out"). None of the
1,687 historical .gate.json records named the code they judged beyond a job id
and a launch-baseline head. The two live consequences:

  * an auto-fix round's tree HEAD was the previous round's seal, so the diff,
    relevance and review all judged a rename DELTA while the record read as a
    verdict on the slice (fixed separately by autofix_chain_base);
  * nothing stopped a verdict being applied to a worktree that changed after it
    was written (a hand edit, a continuation round, an uncommitted fix sitting on
    top of a sealed misspelled round) -- auto-apply / the slicer's land route
    would copy or commit code no gate ever saw.

So every verdict now carries a `judged` stamp: the diff's sha256, the base it
was cut from, the worktree HEAD, and the sha256 of every PRODUCT file the diff
touched, read from disk at judge time. `identity_check()` recomputes those file
hashes; any difference means the verdict is STALE for the code on disk and must
be re-gated before anything lands on its strength.

PURE apart from reading files and `git rev-parse` (read-only, GIT_OPTIONAL_LOCKS=0).
No writes. Consumers: gate-on-complete.py (stamp + apply_fix refusal),
ollama-dispatch-slice (land route refusal), gate-audit.py (stale count).

  python3 gate_identity.py --self-test
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from pathlib import Path

os.environ.setdefault("GIT_OPTIONAL_LOCKS", "0")

STAMP_VERSION = 1
_HEADER = re.compile(r"^(?:\+\+\+|---) (?:[ab]/)?(.+?)\s*$")


def _sha(p: Path):
    try:
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()
    except FileNotFoundError:
        return "absent"
    except Exception:
        return None


def diff_paths(diff_text: str) -> list:
    out = []
    for ln in diff_text.splitlines():
        m = _HEADER.match(ln)
        if m and m.group(1) != "/dev/null" and m.group(1) not in out:
            out.append(m.group(1))
    return out


def _toplevel(cwd):
    try:
        r = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip())
    except Exception:
        pass
    return None


def _head(cwd):
    try:
        r = subprocess.run(["git", "-C", str(cwd), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=30)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def judged_identity(job_id, cwd, diff_path, base="", product_filter=None) -> dict:
    """The stamp. `product_filter(paths) -> set` keeps only product files (the
    gate passes its own product_files); default keeps every path."""
    diff_path = Path(diff_path)
    try:
        txt = diff_path.read_text(errors="replace")
    except Exception:
        txt = ""
    paths = diff_paths(txt)
    keep = set(product_filter(paths)) if product_filter else set(paths)
    top = _toplevel(cwd) or Path(cwd)
    return {"v": STAMP_VERSION, "job_id": job_id,
            "diff_sha256": hashlib.sha256(txt.encode()).hexdigest() if txt else None,
            "base": base or None, "head": _head(cwd), "toplevel": str(top),
            "files": {p: _sha(top / p) for p in sorted(keep)},
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def identity_check(payload: dict, cwd=None) -> tuple:
    """(ok, reasons). ok only when the stamp exists, names at least one file, and
    every judged file hashes the same on disk now. FAIL-CLOSED: no stamp, an
    unreadable file, or a vanished worktree is NOT ok."""
    j = (payload or {}).get("judged") or {}
    files = j.get("files") or {}
    if not j or not isinstance(files, dict):
        return False, ["the verdict carries no judged-identity stamp (written before "
                       "2026-10-06, or by a skipped gate) -- it cannot be shown to "
                       "describe the code on disk; re-gate before landing on it"]
    if not files:
        return False, ["the judged-identity stamp names no product file -- nothing "
                       "to compare; re-gate before landing on it"]
    top = None
    if cwd:
        top = _toplevel(cwd)
    if top is None and j.get("toplevel"):
        top = Path(j["toplevel"])
    if top is None or not top.is_dir():
        return False, [f"the judged worktree is gone ({j.get('toplevel') or cwd})"]
    bad = []
    for rel, h in files.items():
        now = _sha(top / rel)
        if now is None or h is None or now != h:
            bad.append(rel)
    if bad:
        return False, [f"{len(bad)} judged file(s) changed on disk since the verdict was "
                       f"written ({', '.join(bad[:5])}{'...' if len(bad) > 5 else ''}) -- "
                       f"the verdict is STALE for this code; re-gate before landing"]
    return True, []


def _self_test() -> int:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok &= good
        print(("PASS " if good else "FAIL ") + name + ("" if good else f": got {got!r} want {want!r}"))

    td = Path(tempfile.mkdtemp(prefix="gate-identity-st-"))
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", str(td)], check=True, env=env)
    (td / "app.py").write_text("x = 1\n")
    (td / "TASK.md").write_text("task\n")
    subprocess.run(["git", "-C", str(td), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(td), "commit", "-qm", "i"], check=True, env=env)
    (td / "app.py").write_text("x = 2\n")
    d = td / "j.diff"
    d.write_text("diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n"
                 "-x = 1\n+x = 2\n"
                 "diff --git a/TASK.md b/TASK.md\n--- a/TASK.md\n+++ b/TASK.md\n@@ -1 +1 @@\n-task\n+t2\n")
    st = judged_identity("j1", td, d, base="abc",
                         product_filter=lambda ps: {p for p in ps if p != "TASK.md"})
    check("stamp names only product files", sorted(st["files"]), ["app.py"])
    check("stamp carries diff sha + head", (bool(st["diff_sha256"]), bool(st["head"])), (True, True))
    check("unchanged tree -> identity ok", identity_check({"judged": st}, td), (True, []))
    (td / "app.py").write_text("x = 3\n")
    okk, why = identity_check({"judged": st}, td)
    check("edited judged file -> STALE", (okk, "STALE" in why[0]), (False, True))
    (td / "app.py").write_text("x = 2\n")
    check("restored content -> ok again", identity_check({"judged": st}, td)[0], True)
    (td / "app.py").unlink()
    check("deleted judged file -> stale", identity_check({"judged": st}, td)[0], False)
    check("no stamp -> fail closed", identity_check({}, td)[0], False)
    check("empty file list -> fail closed",
          identity_check({"judged": {"files": {}, "toplevel": str(td)}}, td)[0], False)
    check("gone worktree -> fail closed",
          identity_check({"judged": dict(st, toplevel=str(td / "nope"))}, None)[0], False)
    print("SELF-TEST", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(_self_test() if "--self-test" in sys.argv else 0)
