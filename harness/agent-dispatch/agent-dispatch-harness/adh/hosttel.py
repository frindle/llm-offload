"""Host telemetry — one machine-parseable sample of the memory state that
decides whether the next model load will page.

WHY THIS EXISTS
---------------
Without a per-run record of host state you cannot distinguish "this model is
slow" from "this host was degraded when this model ran". Both write the same
thing into a duration column, and the second one is not a property of the model
at all.

The failure that motivated this: one cell was measured at 44 -> 42 -> 53 -> 86
seconds per iteration across four repetitions of an *identical* configuration
whose context never exceeded 30% of the window, so context growth could not
explain it. Rep number was silently confounded with host state. A recovery run
with the wall-clock budget doubled then timed out anyway — 171 s/iteration
against 56 s/iteration for the same cell on a fresh host. Measured live during
that run: 12.3 GB of swap in use and 5.8 GB available out of 64 GB. Raising the
wall does not fix a host in that state; it only moves the failure later.

So: sample host state either side of every run and put it in the results row,
rather than inferring it from durations afterwards.

WHAT TO GATE ON
---------------
`available`, never `free`, and never swap.

  * `free` alone reads near zero on any busy machine and means nothing —
    inactive and purgeable pages are reclaimable under pressure.
  * swap is a LAGGING signal. macOS does not shrink swap files promptly when
    pages are released, so swap staying high after a restart does not mean
    memory is still unavailable. Gating on it would block for minutes after the
    memory was already back. Swap is recorded, never gated on.

PLATFORM
--------
macOS only, via `vm_stat` and `sysctl`. This is a real limitation and is stated
plainly in the README rather than papered over: `probe()` returns `supported=
False` on other platforms and the caller degrades to "telemetry unavailable"
instead of inventing numbers. A Linux implementation reading /proc/meminfo
(MemAvailable, SwapTotal-SwapFree) is a small, welcome contribution.
"""
from __future__ import annotations

import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict

MB = 1048576


@dataclass
class HostSample:
    supported: bool
    platform: str
    total_mb: int = -1
    available_mb: int = -1
    free_mb: int = -1
    swap_used_mb: int = -1
    load1: float = -1.0

    def as_dict(self) -> dict:
        return asdict(self)


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout
    except Exception:                                                # noqa: BLE001
        return ""


def _macos_vm_stat() -> tuple[int, int]:
    out = _run(["/usr/bin/vm_stat"])
    m = re.search(r"page size of (\d+)", out)
    if not m:
        return -1, -1
    page = int(m.group(1))

    def pages(label: str) -> int:
        mm = re.search(re.escape(label) + r":\s+(\d+)", out)
        return int(mm.group(1)) if mm else 0

    # Reclaimable under pressure: free + inactive + speculative + purgeable.
    # This is the standard "available" approximation, and it is the quantity
    # that predicts whether the next model load fits without paging.
    avail = (pages("Pages free") + pages("Pages inactive")
             + pages("Pages speculative") + pages("Pages purgeable"))
    return round(avail * page / MB), round(pages("Pages free") * page / MB)


def _macos_swap_used_mb() -> int:
    out = _run(["/usr/sbin/sysctl", "-n", "vm.swapusage"])
    m = re.search(r"used\s*=\s*([\d.]+)M", out)
    return round(float(m.group(1))) if m else -1


def _macos_load1() -> float:
    out = _run(["/usr/sbin/sysctl", "-n", "vm.loadavg"])
    parts = out.replace("{", "").replace("}", "").split()
    try:
        return float(parts[0])
    except (IndexError, ValueError):
        return -1.0


def _macos_total_mb() -> int:
    out = _run(["/usr/sbin/sysctl", "-n", "hw.memsize"]).strip()
    return round(int(out) / MB) if out.isdigit() else -1


def supported() -> bool:
    return platform.system() == "Darwin" and shutil.which("vm_stat") is not None


def probe() -> HostSample:
    """One sample of host memory state. Never raises: an unsupported platform
    returns supported=False with -1 fields, and callers record that honestly
    rather than substituting a plausible-looking number."""
    if not supported():
        return HostSample(supported=False, platform=platform.system())
    avail, free = _macos_vm_stat()
    return HostSample(
        supported=True,
        platform=platform.system(),
        total_mb=_macos_total_mb(),
        available_mb=avail,
        free_mb=free,
        swap_used_mb=_macos_swap_used_mb(),
        load1=_macos_load1(),
    )


def main(argv: list[str]) -> int:
    s = probe()
    if argv and argv[0] == "--available":
        print(s.available_mb)
    elif argv and argv[0] == "--total":
        print(s.total_mb)
    elif argv and argv[0] == "--json":
        import json
        print(json.dumps(s.as_dict()))
    else:
        print(f"swap={s.swap_used_mb}MB avail={s.available_mb}MB "
              f"free={s.free_mb}MB total={s.total_mb}MB load1={s.load1} "
              f"supported={s.supported}")
    return 0 if s.supported else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
