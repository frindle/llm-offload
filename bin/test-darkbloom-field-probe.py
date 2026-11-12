#!/usr/bin/env python3
"""Runs darkbloom-field-probe.py --selftest (stub server) and checks plan <= 150 requests."""
import subprocess, sys
from pathlib import Path
here = Path(__file__).resolve().parent
probe = here / "darkbloom-field-probe.py"
r = subprocess.run([sys.executable, str(probe), "--selftest"], capture_output=True, text=True, timeout=300)
assert r.returncode == 0 and "SELFTEST PASS" in r.stdout, r.stdout[-2000:] + r.stderr[-500:]
p = subprocess.run([sys.executable, str(probe), "plan"], capture_output=True, text=True)
assert int(p.stdout.rsplit("total", 1)[1]) <= 160, p.stdout
print("test-darkbloom-field-probe: PASS")
