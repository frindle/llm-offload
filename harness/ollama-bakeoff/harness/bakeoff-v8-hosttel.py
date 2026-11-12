#!/usr/bin/env python3
"""Host telemetry for the v8 bake-off — one line, machine-parseable.

WHY THIS EXISTS (Fable blocker B3, 2026-08-23)
----------------------------------------------
v7 could not distinguish "this model is slow" from "this host was degraded when
this model ran". The same cell -- qwen3.8:27b-q8_0 / clamshell, identical config
-- went 44 -> 42 -> 53 -> 86 s/iteration across prelim/r1/r2/r3, and clamshell
peaks at 30% context, so context growth cannot explain it. Rep number was
confounded with host state and the CSV had no column that showed it.

Then the recovery run settled it. qwen3.8 / photo at ctx 65536 with the wall
RAISED to 3600s timed out anyway: 21 iterations in 3600s = 171 s/iteration,
against 56 s/iteration for the same cell on a fresh host in v7 rep 1. Measured
live during that run: swap 12354 MB of 14336, available memory 5857 MB of 65536.
Raising the wall does not fix a host in that state; it just moves the failure.

So v8 records host state per RUN rather than inferring it from durations
afterwards. `available` is the number that matters -- free alone reads ~72 MB on
a busy Mac and means nothing, because inactive and purgeable pages are
reclaimable. Swap is reported too, but it is a LAGGING signal: macOS does not
shrink swap files promptly when pages are freed, so swap staying high after a
restart does NOT mean memory is still unavailable. Never gate on swap. Gate on
available.
"""
import re
import subprocess
import sys


def _vm_stat():
    out = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True).stdout
    page = int(re.search(r"page size of (\d+)", out).group(1))

    def pages(label):
        m = re.search(label + r":\s+(\d+)", out)
        return int(m.group(1)) if m else 0

    # Reclaimable under pressure: free + inactive + speculative + purgeable.
    # This is the standard "available" approximation and it is what predicts
    # whether the next model load will fit without paging.
    avail = pages("Pages free") + pages("Pages inactive") \
        + pages("Pages speculative") + pages("Pages purgeable")
    return round(avail * page / 1048576), round(pages("Pages free") * page / 1048576)


def _swap_mb():
    out = subprocess.run(["/usr/sbin/sysctl", "-n", "vm.swapusage"],
                         capture_output=True, text=True).stdout
    m = re.search(r"used\s*=\s*([\d.]+)M", out)
    return round(float(m.group(1))) if m else -1


def _load1():
    out = subprocess.run(["/usr/sbin/sysctl", "-n", "vm.loadavg"],
                         capture_output=True, text=True).stdout
    parts = out.replace("{", "").replace("}", "").split()
    return parts[0] if parts else "-1"


def _total_mb():
    out = subprocess.run(["/usr/sbin/sysctl", "-n", "hw.memsize"],
                         capture_output=True, text=True).stdout.strip()
    return round(int(out) / 1048576) if out.isdigit() else -1


def main():
    avail, free = _vm_stat()
    if len(sys.argv) > 1 and sys.argv[1] == "--available":
        print(avail)          # for the readiness gate, which wants one integer
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "--total":
        print(_total_mb())
        return 0
    # swap_mb|avail_mb|free_mb|load1 -- fixed order, the lib splits on '|'
    print(f"{_swap_mb()}|{avail}|{free}|{_load1()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
