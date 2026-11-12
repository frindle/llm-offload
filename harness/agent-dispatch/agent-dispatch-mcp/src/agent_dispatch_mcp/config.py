"""Configuration. Everything host-specific lives here or in the environment.

There are no hostnames, IPs, paths or model inventories baked into any other
module in this package. That is deliberate and architectural, not a tidying
pass: it is what makes the server publishable and what makes someone else's
machine a first-class citizen rather than a special case.

Resolution order for the config file:
  1. explicit path passed to ``load``
  2. ``$AGENT_DISPATCH_CONFIG``
  3. ``./config.toml``
  4. ``$XDG_CONFIG_HOME/agent-dispatch-mcp/config.toml``
     (or ``~/.config/agent-dispatch-mcp/config.toml``)
  5. built-in defaults

Individual settings can also be overridden by environment variable, which is the
right channel for a hostname or endpoint you do not want on disk:
  AGENT_DISPATCH_ENDPOINT, AGENT_DISPATCH_KV_MB_PER_1K,
  AGENT_DISPATCH_WEIGHTS_HEADROOM, AGENT_DISPATCH_PHYSICAL_CAP,
  AGENT_DISPATCH_WAIT_TIMEOUT_S, AGENT_DISPATCH_POLL_INTERVAL_S,
  AGENT_DISPATCH_WORKSPACE, AGENT_DISPATCH_DEFAULT_NUM_CTX
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# MEASURED ON ONE SPECIFIC MACHINE. Read this before trusting a row.
#
# These are resident-set sizes observed for these models under one runtime, one
# quantisation and one host. They are shipped as a STARTING TABLE, not as facts
# about the models. Resident size varies with the runtime, the quantisation, the
# KV cache type, whether the runtime memory-maps weights, and how much of the
# model is offloaded. If your numbers differ, yours are right and these are
# wrong -- override them in config.
#
# Get your own with `resident_probe` (see README): load the model, then read the
# runtime's reported resident size.
#
# Any model not listed falls back to `unknown_model_resident_mb`, which is
# deliberately conservative: over-demanding costs a wait, under-demanding costs a
# silently paging run whose timings mean nothing.
DEFAULT_RESIDENT_MB: dict[str, int] = {
    # 27B dense, q8_0
    "qwen3.8:27b-q8_0": 30000,
    # ~30B MoE, ~3B active, Q4_K_M
    "qwen3-coder:30b": 18600,
    # large MoE, Q4_K_M
    "qwen3-coder-next:q4_K_M": 51700,
    # 32B dense
    "deepseek-r1:32b": 19900,
    # 14B dense
    "qwen3-14b-agentic": 9300,
    "qwen2.5-coder:14b": 9000,
}


@dataclass
class GateConfig:
    # resident * this covers weights plus load-time headroom.
    weights_headroom: float = 1.2
    # MB of KV cache per 1k tokens of configured context. See gating.py for the
    # full derivation and for why this was once 2 and why that was catastrophic.
    kv_mb_per_1k: float = 150.0
    # Never demand more than this fraction of physical memory.
    physical_cap_fraction: float = 0.85
    # Bounded wait. Unbounded waits hang batches.
    wait_timeout_s: int = 240
    poll_interval_s: int = 5
    unknown_model_resident_mb: int = 20000


@dataclass
class DispatchConfig:
    # Inference endpoint. Host-specific -- keep it in config or the environment,
    # never in code. Default is a loopback Ollama.
    endpoint: str = "http://127.0.0.1:11434"
    default_num_ctx: int = 32768
    request_timeout_s: int = 1800
    max_iterations: int = 25
    # Directory the verifier runs in. Must be set explicitly to run a verifier.
    workspace: str | None = None


@dataclass
class Config:
    gate: GateConfig = field(default_factory=GateConfig)
    dispatch: DispatchConfig = field(default_factory=DispatchConfig)
    resident_mb: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_RESIDENT_MB))
    # Extra verifier command fragments, appended to the built-in list.
    extra_verifiers: list[str] = field(default_factory=list)
    source_path: str | None = None

    def resident_mb_for(self, model: str) -> tuple[int, str]:
        """Return (resident_mb, source). Source says where the number came from,
        so a caller can tell a measured value from a conservative guess."""
        if model in self.resident_mb:
            src = "config" if self.source_path else "shipped_default_table"
            return int(self.resident_mb[model]), src
        # Tolerate an :latest suffix mismatch in either direction.
        base = model.split(":")[0]
        for k, v in self.resident_mb.items():
            if k.split(":")[0] == base:
                return int(v), "config_prefix_match"
        return self.gate.unknown_model_resident_mb, "unknown_model_fallback"


def _candidate_paths(explicit: str | None) -> list[Path]:
    out = []
    if explicit:
        out.append(Path(explicit))
    env = os.environ.get("AGENT_DISPATCH_CONFIG")
    if env:
        out.append(Path(env))
    out.append(Path.cwd() / "config.toml")
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    out.append(base / "agent-dispatch-mcp" / "config.toml")
    return out


def _env_float(name: str, cur: float) -> float:
    v = os.environ.get(name)
    try:
        return float(v) if v else cur
    except ValueError:
        return cur


def _env_int(name: str, cur: int) -> int:
    v = os.environ.get(name)
    try:
        return int(v) if v else cur
    except ValueError:
        return cur


def load(path: str | None = None) -> Config:
    cfg = Config()
    for p in _candidate_paths(path):
        if p.is_file():
            raw = tomllib.loads(p.read_text())
            g = raw.get("gate", {})
            for k in vars(cfg.gate):
                if k in g:
                    setattr(cfg.gate, k, g[k])
            d = raw.get("dispatch", {})
            for k in vars(cfg.dispatch):
                if k in d:
                    setattr(cfg.dispatch, k, d[k])
            if raw.get("resident_mb"):
                # Replace wholesale when provided: a user's measured table should
                # not be silently mixed with numbers from another machine.
                cfg.resident_mb = {str(k): int(v) for k, v in raw["resident_mb"].items()}
            cfg.extra_verifiers = list(raw.get("verifiers", {}).get("extra", []))
            cfg.source_path = str(p)
            break

    # Environment overrides win over the file.
    cfg.gate.kv_mb_per_1k = _env_float("AGENT_DISPATCH_KV_MB_PER_1K", cfg.gate.kv_mb_per_1k)
    cfg.gate.weights_headroom = _env_float(
        "AGENT_DISPATCH_WEIGHTS_HEADROOM", cfg.gate.weights_headroom
    )
    cfg.gate.physical_cap_fraction = _env_float(
        "AGENT_DISPATCH_PHYSICAL_CAP", cfg.gate.physical_cap_fraction
    )
    cfg.gate.wait_timeout_s = _env_int("AGENT_DISPATCH_WAIT_TIMEOUT_S", cfg.gate.wait_timeout_s)
    cfg.gate.poll_interval_s = _env_int(
        "AGENT_DISPATCH_POLL_INTERVAL_S", cfg.gate.poll_interval_s
    )
    cfg.dispatch.endpoint = os.environ.get("AGENT_DISPATCH_ENDPOINT", cfg.dispatch.endpoint)
    cfg.dispatch.workspace = os.environ.get("AGENT_DISPATCH_WORKSPACE", cfg.dispatch.workspace)
    cfg.dispatch.default_num_ctx = _env_int(
        "AGENT_DISPATCH_DEFAULT_NUM_CTX", cfg.dispatch.default_num_ctx
    )
    return cfg
