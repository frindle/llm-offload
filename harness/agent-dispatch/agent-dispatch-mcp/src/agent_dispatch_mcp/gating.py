"""Host-readiness gating: refuse or wait, never thrash.

THE RULE
--------
    threshold_mb = resident_mb * WEIGHTS_HEADROOM + ctx_tokens/1024 * KV_MB_PER_1K
    threshold_mb = min(threshold_mb, total_mb * PHYSICAL_CAP_FRACTION)

and the gate compares that against **available** memory (see ``telemetry``).

WHY MODEL-RELATIVE, NOT A FLAT FRACTION
---------------------------------------
A flat "40% of physical" gate is wrong in both directions at once. It
over-demands for a ~9GB model, which then eats a spurious wait and gets flagged
degraded on every single run; and it under-demands for a ~30GB model with a 64k
KV cache, where 40% free is nowhere near enough to load without paging. The
gate's job is to predict whether the *next model load* fits, and that is
inherently a property of the model.

THE KV CONSTANT, AND WHY IT IS 150
-----------------------------------
This constant was originally 2 MB per 1k tokens. That was **wrong by roughly
75x**, and the error was not cosmetic.

2 MB/1k tokens is 2 KB per token. The real figure for a model in this size class
-- roughly 64 layers, 8 GQA KV heads, head_dim 128, q8_0 K and V cache -- is:

    2 (K and V) * 64 layers * 8 heads * 128 dims * 1 byte = 128 KB per token
                                                          ~ 128 MB per 1k tokens
                                                          ~ 8.4 GB at 65536 ctx

The old constant allotted 128 MB for that entire 8.4 GB. Because the gate exists
precisely to predict paging, and the runs that motivated it were exactly the big
models at 64k context, a 75x-short KV term meant ``host_ready=yes`` could be
stamped on a run that was about to page -- i.e. the gate was certifying clean the
exact confound it was built to detect. Corroborated in practice: a ~30GB model at
64k drove ~12 GB into swap while the gate reported the host ready.

150 rather than 128 deliberately errs safe. Head counts and quantisation vary
across a roster; a too-generous gate costs a wait, a too-tight one costs a
silently invalid measurement. Both constants are configurable.

THE 85% CAP
-----------
Never demand more than 85% of physical memory. A threshold the host cannot reach
even when completely idle makes every run wait out the full timeout and report
"not ready" universally, which destroys the signal rather than protecting
anything. The cap only binds for very large context configurations.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass

from .config import Config
from .telemetry import HostState, HostTelemetry


@dataclass(frozen=True)
class Threshold:
    model: str
    num_ctx: int
    resident_mb: int
    resident_source: str
    weights_term_mb: int
    kv_term_mb: int
    raw_threshold_mb: int
    physical_cap_mb: int
    capped: bool
    threshold_mb: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class GateResult:
    ready: bool
    threshold: Threshold
    host: HostState
    shortfall_mb: int
    waited_s: float
    polls: int
    reason: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["threshold"] = self.threshold.to_dict()
        d["host"] = self.host.to_dict()
        return d


def compute_threshold(cfg: Config, tel: HostTelemetry, model: str, num_ctx: int) -> Threshold:
    resident_mb, source = cfg.resident_mb_for(model)
    weights = int(resident_mb * cfg.gate.weights_headroom)
    kv = int(num_ctx * cfg.gate.kv_mb_per_1k / 1024)
    raw = weights + kv
    total = tel.total_mb()
    cap = int(total * cfg.gate.physical_cap_fraction)
    capped = raw > cap > 0
    return Threshold(
        model=model,
        num_ctx=num_ctx,
        resident_mb=resident_mb,
        resident_source=source,
        weights_term_mb=weights,
        kv_term_mb=kv,
        raw_threshold_mb=raw,
        physical_cap_mb=cap,
        capped=capped,
        threshold_mb=cap if capped else raw,
    )


def check_ready(cfg: Config, tel: HostTelemetry, model: str, num_ctx: int) -> GateResult:
    """Single-shot gate check. No waiting."""
    th = compute_threshold(cfg, tel, model, num_ctx)
    host = tel.snapshot()
    short = max(0, th.threshold_mb - host.available_mb)
    ready = host.available_mb >= th.threshold_mb
    return GateResult(
        ready=ready,
        threshold=th,
        host=host,
        shortfall_mb=short,
        waited_s=0.0,
        polls=1,
        reason=(
            f"available {host.available_mb}MB >= threshold {th.threshold_mb}MB"
            if ready
            else f"available {host.available_mb}MB is {short}MB short of "
            f"threshold {th.threshold_mb}MB"
        ),
    )


def wait_for_ready(
    cfg: Config,
    tel: HostTelemetry,
    model: str,
    num_ctx: int,
    timeout_s: int | None = None,
    poll_s: int | None = None,
    sleep=time.sleep,
) -> GateResult:
    """Poll the gate until ready or the (bounded) timeout expires.

    The wait is always bounded. An unbounded wait hangs a whole batch on one bad
    host, which is a worse failure than dispatching with an honest not-ready flag.
    """
    timeout_s = cfg.gate.wait_timeout_s if timeout_s is None else timeout_s
    poll_s = cfg.gate.poll_interval_s if poll_s is None else poll_s
    th = compute_threshold(cfg, tel, model, num_ctx)

    started = time.monotonic()
    polls = 0
    host = tel.snapshot()
    while True:
        polls += 1
        host = tel.snapshot()
        if host.available_mb >= th.threshold_mb:
            break
        if time.monotonic() - started >= timeout_s:
            break
        sleep(min(poll_s, max(0.0, timeout_s - (time.monotonic() - started))))

    waited = round(time.monotonic() - started, 2)
    ready = host.available_mb >= th.threshold_mb
    short = max(0, th.threshold_mb - host.available_mb)
    return GateResult(
        ready=ready,
        threshold=th,
        host=host,
        shortfall_mb=short,
        waited_s=waited,
        polls=polls,
        reason=(
            f"ready after {waited}s: available {host.available_mb}MB >= "
            f"threshold {th.threshold_mb}MB"
            if ready
            else f"gave up after {waited}s: available {host.available_mb}MB is "
            f"{short}MB short of threshold {th.threshold_mb}MB"
        ),
    )
