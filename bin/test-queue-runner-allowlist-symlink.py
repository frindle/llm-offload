#!/usr/bin/env python3
"""test-queue-runner-allowlist-symlink.py -- an allowlisted --runner must stay allowed
when HOME has a symlink in its path.

Found by pipeline-canary.py (2026-10-06). The canary's temp HOME lived under
/var/folders/... (macOS: /var -> /private/var). enqueue resolves --runner, but
ALLOWED_RUNNERS held the unresolved Path.home() strings, so every gate review was
"enqueue-failed: runner not allowlisted". The same holds for any symlinked HOME or
~/bin on a real machine.

The module is loaded under a temp HOME reached through a symlink. Only the module
is imported: nothing is enqueued and no state is read or written.

Usage:  test-queue-runner-allowlist-symlink.py [--queue PATH]   (default ~/bin/ollama-queue.py)
The revert test passes the .bak and must FAIL. Exit 0 = pass.
"""
import argparse
import importlib.machinery
import importlib.util
import os
import sys
import tempfile
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default=str(Path.home() / "bin" / "ollama-queue.py"))
    a = ap.parse_args()
    real = Path(tempfile.mkdtemp(prefix="runner-allow-")).resolve()
    (real / "home" / "bin").mkdir(parents=True)
    (real / "home" / "bin" / "code-review-agent.py").write_text("# stub\n")
    link = real / "link"
    link.symlink_to(real / "home")
    os.environ["HOME"] = str(link)
    os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
    ld = importlib.machinery.SourceFileLoader("oq_allow", a.queue)
    spec = importlib.util.spec_from_loader(ld.name, ld)
    q = importlib.util.module_from_spec(spec)
    sys.argv = [a.queue]
    spec.loader.exec_module(q)
    runner = str((link / "bin" / "code-review-agent.py").resolve())   # what enqueue checks
    ok = True
    for name, cond in (
            ("the resolved runner path (what enqueue compares) is allowlisted",
             runner in q.ALLOWED_RUNNERS),
            ("the exact Path.home() entry is still there",
             str(link / "bin" / "code-review-agent.py") in q.ALLOWED_RUNNERS),
            ("nothing outside the allowlisted names became runnable",
             all(Path(p).name in {Path(x).name for x in q.ALLOWED_RUNNERS} for p in q.ALLOWED_RUNNERS)
             and not any(Path(p).name == "ollama-worker.py" for p in q.ALLOWED_RUNNERS))):
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond
    print("ALL PASS" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
