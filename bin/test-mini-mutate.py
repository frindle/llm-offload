#!/usr/bin/env python3
"""mini_mutate (lever-1 line-scoped mutation score): runs its built-in self-test (strong test outscores a weak one; canary-survives and
fails-on-ref are unscored, never a REJECT; the work tree is never written; no mutant lands outside the ref-changed lines)."""
import subprocess, sys
from pathlib import Path
p = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "mini_mutate.py"), "--self-test"], capture_output=True, text=True, timeout=600)
print(p.stdout.strip() or p.stderr[-800:])
sys.exit(p.returncode)
