"""Host memory telemetry.

WHY AVAILABLE, AND NEVER SWAP
-----------------------------
This module exists to answer one question: *will the next model load fit
without paging?* Everything else it reports is diagnostic.

`free` alone is useless. On a busy machine it reads in the tens of megabytes
and means nothing, because inactive, speculative and purgeable pages are all
reclaimable under pressure. The number that predicts a clean load is
**available** = free + inactive + speculative + purgeable.

Swap is reported but is a LAGGING signal and must never be gated on. macOS does
not shrink its swap files promptly when pages are freed, so swap can sit high
for a long time after the memory is genuinely back. A swap gate therefore blocks
runs that would have been fine, which trains people to disable the gate.

PLATFORM SUPPORT
----------------
macOS is implemented (``vm_stat`` + ``sysctl``). Linux is a clean drop-in: add a
``LinuxTelemetry`` backend implementing the same three methods and register it in
``get_backend``. The rest of the server touches only the ``HostTelemetry``
protocol and the ``HostState`` dataclass, so no other file needs to change.
"""

from __future__ import annotations

import platform
import re
import subprocess
from dataclasses import asdict, dataclass
from typing import Protocol


@dataclass(frozen=True)
class HostState:
    """One snapshot of host memory pressure. All memory figures are MB."""

    available_mb: int
    free_mb: int
    total_mb: int
    swap_used_mb: int
    load1: float
    platform: str

    def to_dict(self) -> dict:
        return asdict(self)


class TelemetryUnavailable(RuntimeError):
    """Raised when no telemetry backend exists for the current platform."""


class HostTelemetry(Protocol):
    def available_mb(self) -> int: ...
    def total_mb(self) -> int: ...
    def snapshot(self) -> HostState: ...


def _run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=False).stdout


class MacTelemetry:
    """macOS backend. Reads ``vm_stat`` and ``sysctl``; no third-party deps."""

    name = "darwin"

    def _vm_stat(self) -> tuple[int, int]:
        out = _run(["/usr/bin/vm_stat"])
        m = re.search(r"page size of (\d+)", out)
        if not m:
            raise TelemetryUnavailable("could not parse vm_stat page size")
        page = int(m.group(1))

        def pages(label: str) -> int:
            mm = re.search(label + r":\s+(\d+)", out)
            return int(mm.group(1)) if mm else 0

        avail_pages = (
            pages("Pages free")
            + pages("Pages inactive")
            + pages("Pages speculative")
            + pages("Pages purgeable")
        )
        return (
            round(avail_pages * page / 1048576),
            round(pages("Pages free") * page / 1048576),
        )

    def _swap_used_mb(self) -> int:
        out = _run(["/usr/sbin/sysctl", "-n", "vm.swapusage"])
        m = re.search(r"used\s*=\s*([\d.]+)M", out)
        return round(float(m.group(1))) if m else -1

    def _load1(self) -> float:
        out = _run(["/usr/sbin/sysctl", "-n", "vm.loadavg"])
        parts = out.replace("{", "").replace("}", "").split()
        try:
            return float(parts[0])
        except (IndexError, ValueError):
            return -1.0

    def total_mb(self) -> int:
        out = _run(["/usr/sbin/sysctl", "-n", "hw.memsize"]).strip()
        return round(int(out) / 1048576) if out.isdigit() else -1

    def available_mb(self) -> int:
        return self._vm_stat()[0]

    def snapshot(self) -> HostState:
        avail, free = self._vm_stat()
        return HostState(
            available_mb=avail,
            free_mb=free,
            total_mb=self.total_mb(),
            swap_used_mb=self._swap_used_mb(),
            load1=self._load1(),
            platform=self.name,
        )


def get_backend(system: str | None = None) -> HostTelemetry:
    system = (system or platform.system()).lower()
    if system == "darwin":
        return MacTelemetry()
    raise TelemetryUnavailable(
        f"no host-telemetry backend for platform {system!r}. "
        "Only macOS is implemented. A Linux backend is a clean drop-in: implement "
        "available_mb/total_mb/snapshot (e.g. from /proc/meminfo MemAvailable) and "
        "register it in telemetry.get_backend."
    )
