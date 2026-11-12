"""Model-relative host-readiness gating.

THE PROBLEM
-----------
A benchmark run on a host that is already paging measures the host, not the
model. The confound is invisible in the results table — it arrives as a larger
number in the duration column, indistinguishable from a slower model. Repetition
does not average it out either, because host degradation accumulates *across* a
round: later repetitions are systematically slower than earlier ones, so rep
index becomes a hidden covariate.

THE GATE
--------
Before every run: restart the inference server (if this harness owns it), then
wait for available memory to recover past a threshold computed for *this model
at this context*. Dispatch either way, but record which happened as
`host_ready`, so a run made on a still-degraded host is visible in the data
rather than reconstructed from durations months later.

WHY MODEL-RELATIVE
------------------
A flat "40% of physical" threshold is wrong in both directions simultaneously:

  * It over-demands for a small model. A 9 GB model on a 64 GB host would wait
    the full timeout and be flagged not-ready on every single run, for memory it
    was never going to need.
  * It under-demands for a large one. A 30 GB model with a 64k KV cache does not
    fit in 26 GB, so the gate would stamp `host_ready=yes` on a run that was
    about to page — certifying the exact confound the flag exists to expose.

The threshold is therefore `resident_mb * headroom + kv_allowance(num_ctx)`,
capped so it can never exceed what the host could reach even when idle.

THE KV TERM IS NOT A ROUNDING ERROR
-----------------------------------
An early version of this used 2 MB per 1k tokens. That is 2 KB per token, and
for a model in the 30B class — roughly 64 layers, 8 grouped-query KV heads, head
dimension 128, 8-bit K and V cache — the real figure is

    2 (K and V) * 64 layers * 8 heads * 128 dims * 1 byte = 128 KB per token

i.e. ~128 MB per 1k tokens, about 8.4 GB at a 64k window. The old constant
budgeted 128 MB for that: short by roughly seventy-five times. It mattered
because the cells that motivate gating at all are precisely the large models at
large contexts — the ones where the KV cache is a double-digit fraction of the
footprint. A 75x-short KV term meant the gate passed exactly the runs it existed
to catch, and the corroborating observation was already on record: that model at
a 64k window drove 12 GB into swap.

The shipped default is 150 rather than 128, deliberately on the safe side, since
head counts vary across a roster. A too-generous gate costs a wait. A too-tight
one costs a silently invalid row, and there is no way to tell afterwards which
rows those were.

Every constant here is configurable; none of them are literals in this file's
logic beyond the documented defaults.
"""
from __future__ import annotations

import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import hosttel
from .config import Config, ModelSpec


@dataclass
class GateResult:
    ready: bool
    available_mb: int
    needed_mb: int
    waited_s: float
    reason: str

    @property
    def host_ready(self) -> str:
        return "yes" if self.ready else "no"


def threshold_mb(cfg: Config, model: ModelSpec, num_ctx: int,
                 total_mb: int | None = None) -> int:
    """Available memory this model needs before it is safe to dispatch."""
    g = cfg.section("gate")
    headroom = float(g.get("weights_headroom", 1.2))
    kv_per_1k = float(g.get("kv_mb_per_1k_tokens", 150))
    cap_frac = float(g.get("max_fraction_of_physical", 0.85))

    if model.resident_mb <= 0:
        raise ValueError(
            f"model {model.name!r} has resident_mb=0. Measure it with the model "
            f"loaded and put the real number in the config — the gate is a "
            f"prediction about this specific model and cannot be made from a "
            f"default.")

    kv_mb = int(num_ctx * kv_per_1k / 1024)
    need = int(model.resident_mb * headroom) + kv_mb

    if total_mb is None:
        total_mb = hosttel.probe().total_mb
    if total_mb and total_mb > 0:
        # A threshold the host cannot reach even when idle would make every run
        # wait the full timeout and report host_ready=no universally, which
        # destroys the flag's meaning instead of protecting anything.
        need = min(need, int(total_mb * cap_frac))
    return need


def classify(cfg: Config, model: ModelSpec, num_ctx: int, clean_host_avail_mb: int) -> str:
    """REACHABLE or STRUCTURAL, decided against a *measured* clean-host figure.

    A model whose honest threshold exceeds what the host has when idle will
    never pass the gate. That is not a gating failure, it is a property of the
    hardware, and it is a routing fact in its own right ("this model needs a
    bigger host"). The distinction has to be fixed BEFORE a round dispatches —
    made afterwards it is indistinguishable from explaining away a bad result.
    """
    need = threshold_mb(cfg, model, num_ctx)
    return "REACHABLE" if need <= clean_host_avail_mb else "STRUCTURAL"


def _server_up(host: str, timeout_s: float = 5.0) -> bool:
    url = host.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s):
            return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def restart_server(cfg: Config, log=print) -> bool:
    """Restart the inference server, if this harness owns its lifecycle.

    On a shared or remote endpoint it does not: other things are being served
    from it, and restarting would be an outage someone else notices. That case
    is recorded honestly rather than pretending the host was controlled.
    """
    b = cfg.section("backend")
    if not b.get("owns_server", False):
        log("[gate] endpoint is not owned by this harness — not restarting")
        return False
    cmd = b.get("restart_command")
    if not cmd:
        log("[gate] owns_server=true but no restart_command configured")
        return False

    log(f"[gate] restarting inference server: {cmd}")
    subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)

    host = b.get("host", "")
    deadline = time.time() + float(cfg.get("gate", "server_up_timeout_s", 60))
    while time.time() < deadline:
        time.sleep(2)
        if _server_up(host):
            return True
    log("[gate] server did not come back within its timeout")
    return False


def wait_for_ready(cfg: Config, model: ModelSpec, num_ctx: int, log=print) -> GateResult:
    """Wait, bounded, for available memory to reach this model's threshold.

    Returns rather than raises when the wait expires: the round continues and
    the row is flagged `host_ready=no`. Refusing to dispatch would silently
    delete data from exactly the configurations that stress the host, which is
    a worse bias than recording the degradation.
    """
    if not cfg.get("gate", "enabled", True):
        return GateResult(True, -1, -1, 0.0, "gate disabled in config")

    sample = hosttel.probe()
    if not sample.supported:
        # Do not fabricate readiness on a platform we cannot measure.
        return GateResult(False, -1, -1, 0.0,
                          f"host telemetry unsupported on {sample.platform}")

    need = threshold_mb(cfg, model, num_ctx, total_mb=sample.total_mb)
    budget = float(cfg.get("gate", "wait_seconds", 240))
    poll = float(cfg.get("gate", "poll_seconds", 5))

    started = time.time()
    avail = sample.available_mb
    while avail < need and (time.time() - started) < budget:
        time.sleep(poll)
        avail = hosttel.probe().available_mb

    waited = round(time.time() - started, 1)
    if avail >= need:
        log(f"[gate] READY avail={avail}MB need={need}MB after {waited}s")
        return GateResult(True, avail, need, waited, "threshold met")
    log(f"[gate] DEGRADED avail={avail}MB need={need}MB after {waited}s — "
        f"dispatching anyway, flagged host_ready=no")
    return GateResult(False, avail, need, waited, "threshold not met within budget")


def prepare(cfg: Config, model: ModelSpec, num_ctx: int, log=print) -> GateResult:
    """Full pre-run host control: restart, then gate."""
    restart_server(cfg, log=log)
    return wait_for_ready(cfg, model, num_ctx, log=log)
