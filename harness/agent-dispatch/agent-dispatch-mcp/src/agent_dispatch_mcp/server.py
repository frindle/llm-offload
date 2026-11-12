"""MCP server (stdio transport) exposing the gate, the verifier and the routing table."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from . import __version__, config, routing
from .dispatch import dispatch as _dispatch
from .gating import check_ready, compute_threshold, wait_for_ready
from .telemetry import TelemetryUnavailable, get_backend
from .verify import (
    CLAIM_CLASSES,
    TAXONOMY_CAVEAT,
    behaviour_from_transcript,
    classify_claim,
    run_verifier,
    verifier_list,
)

mcp = MCPServer(
    name="agent-dispatch",
    version=__version__,
    instructions=(
        "Gated, verified dispatch to local inference hosts. Three things a raw "
        "inference API does not do:\n"
        "1. Refuses or waits when the host cannot fit the model at the requested "
        "context, instead of producing a run that pages and reporting its "
        "meaningless timings as data.\n"
        "2. Runs a verifier after the model claims completion and reports whether "
        "the claim held.\n"
        "3. Serves a routing table in which every verdict carries an explicit "
        "status. NO ROW IS CONFIRMED. Never present a `provisional` verdict as "
        "settled, and never read a `void` row's absent verdict as a negative one."
    ),
)

_CFG = config.load()


def _tel():
    return get_backend()


def _cfg_summary() -> dict:
    return {
        "config_file": _CFG.source_path or "(built-in defaults; no config file found)",
        "endpoint": _CFG.dispatch.endpoint,
        "kv_mb_per_1k": _CFG.gate.kv_mb_per_1k,
        "weights_headroom": _CFG.gate.weights_headroom,
        "physical_cap_fraction": _CFG.gate.physical_cap_fraction,
        "wait_timeout_s": _CFG.gate.wait_timeout_s,
        "default_num_ctx": _CFG.dispatch.default_num_ctx,
        "workspace": _CFG.dispatch.workspace,
        "known_models": sorted(_CFG.resident_mb),
        "resident_table_warning": (
            "Resident sizes are measured on one specific machine under one runtime "
            "and quantisation. They are a starting table, not facts about the "
            "models. Override them in config with your own measurements."
        ),
    }


# ---------------------------------------------------------------------------
# 1. Host-readiness gating
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Memory the host must have AVAILABLE before this model can load at this "
        "context without paging. Returns the full arithmetic (weights term, KV "
        "term, physical cap) so the number is auditable rather than opaque."
    )
)
def readiness_threshold(model: str, num_ctx: int = 32768) -> dict[str, Any]:
    try:
        th = compute_threshold(_CFG, _tel(), model, num_ctx)
    except TelemetryUnavailable as e:
        return {"error": str(e)}
    d = th.to_dict()
    d["formula"] = (
        f"min(resident_mb * {_CFG.gate.weights_headroom} + "
        f"num_ctx/1024 * {_CFG.gate.kv_mb_per_1k}, "
        f"total_mb * {_CFG.gate.physical_cap_fraction})"
    )
    d["gated_on"] = (
        "AVAILABLE memory, never swap. Swap is a lagging signal -- macOS does not "
        "shrink swap files promptly when pages are freed, so a swap gate blocks "
        "long after the memory is back."
    )
    if th.resident_source == "unknown_model_fallback":
        d["warning"] = (
            f"{model!r} is not in the resident-size table; using the conservative "
            f"fallback of {th.resident_mb}MB. Measure it and add it to config."
        )
    return d


@mcp.tool(
    description=(
        "Current host memory telemetry. `available_mb` is the number that predicts "
        "whether the next model load fits; `free_mb` alone is close to meaningless "
        "on a busy machine. Swap is reported but must never be gated on."
    )
)
def host_telemetry() -> dict[str, Any]:
    try:
        s = _tel().snapshot()
    except TelemetryUnavailable as e:
        return {"error": str(e)}
    d = s.to_dict()
    d["notes"] = {
        "available_mb": "free + inactive + speculative + purgeable; reclaimable under pressure",
        "free_mb": "diagnostic only; reads near zero on a busy host and means nothing",
        "swap_used_mb": "LAGGING signal. Reported, never gated on.",
    }
    return d


@mcp.tool(
    description=(
        "Check whether the host can fit this model+context right now. Single shot, "
        "no waiting. Returns ready plus the exact shortfall in MB."
    )
)
def check_host_ready(model: str, num_ctx: int = 32768) -> dict[str, Any]:
    try:
        return check_ready(_CFG, _tel(), model, num_ctx).to_dict()
    except TelemetryUnavailable as e:
        return {"error": str(e)}


@mcp.tool(
    description=(
        "Poll until the host can fit this model+context, or the timeout expires. "
        "The wait is always bounded -- an unbounded wait hangs a whole batch on one "
        "bad host, which is a worse failure than an honest not-ready flag."
    )
)
def wait_for_host(
    model: str, num_ctx: int = 32768, timeout_s: int | None = None
) -> dict[str, Any]:
    try:
        return wait_for_ready(_CFG, _tel(), model, num_ctx, timeout_s=timeout_s).to_dict()
    except TelemetryUnavailable as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# 2. Claim-vs-verify
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Dispatch a prompt to the configured inference endpoint behind the host "
        "gate, then run a verifier against the model's claim. Refuses (or waits) "
        "rather than dispatching onto a host that will page. Returns the gate "
        "decision, the verifier result and a claim classification."
    )
)
def gated_dispatch(
    model: str,
    prompt: str,
    num_ctx: int | None = None,
    system: str | None = None,
    verifier_command: str | None = None,
    workspace: str | None = None,
    wait: bool = True,
    force: bool = False,
    files_changed: int | None = None,
    diff_text: str | None = None,
) -> dict[str, Any]:
    try:
        return _dispatch(
            _CFG,
            _tel(),
            model=model,
            prompt=prompt,
            num_ctx=num_ctx,
            system=system,
            verifier_command=verifier_command,
            workspace=workspace,
            wait=wait,
            force=force,
            diff_text=diff_text,
            files_changed=files_changed,
        )
    except TelemetryUnavailable as e:
        return {"error": str(e)}


@mcp.tool(
    description=(
        "Run a verifier command in a workspace and report the result. A verifier "
        "that could not run yields passed=null, never false -- conflating 'did not "
        "run' with 'failed' manufactures fabrication reports out of misconfiguration."
    )
)
def verify_workspace(
    command: str, workspace: str | None = None, timeout_s: int = 900
) -> dict[str, Any]:
    return run_verifier(command, workspace or _CFG.dispatch.workspace, timeout_s).to_dict()


@mcp.tool(
    description=(
        "Classify a completion claim against a verifier result, using the "
        "honest_success / FABRICATED_COMPLETION / unverifiable_claim / "
        "honest_partial / honest_recognition / no_claim taxonomy. THE TAXONOMY IS "
        "PROPOSED AND UNVALIDATED -- no human calibration read has been done."
    )
)
def assess_claim(
    final_message: str,
    verifier_command: str | None = None,
    workspace: str | None = None,
    verifier_passed: bool | None = None,
    verifier_exit_code: int | None = None,
    files_changed: int | None = None,
    stopped_early: bool = False,
) -> dict[str, Any]:
    from .verify import VerifierResult

    if verifier_command and verifier_passed is None:
        ver = run_verifier(verifier_command, workspace or _CFG.dispatch.workspace)
    else:
        ver = VerifierResult(
            ran=verifier_passed is not None,
            command=verifier_command,
            exit_code=verifier_exit_code,
            passed=verifier_passed,
            stdout_tail="",
            stderr_tail="",
            note="supplied by caller" if verifier_passed is not None else "no verifier result",
        )
    return {
        "verifier": ver.to_dict(),
        **classify_claim(final_message, ver, files_changed, stopped_early).to_dict(),
    }


@mcp.tool(
    description=(
        "Behavioural metrics for a run transcript: time_to_first_mutation, "
        "self_verify_count, churn_ratio, diff_magnitude. These separate outcomes "
        "that outcome fields alone record identically -- e.g. files_changed=0 is "
        "written both by a model that explored and never acted and by one that "
        "correctly recognised no change was needed."
    )
)
def score_behaviour(
    messages: list[dict], diff_text: str | None = None
) -> dict[str, Any]:
    b = behaviour_from_transcript(
        messages, verifiers=verifier_list(_CFG.extra_verifiers), diff_text=diff_text
    )
    return {
        "metrics": b.to_dict(),
        "self_verify_note": (
            "self_verify_count is computed from a WHITELIST of verifier command "
            "fragments and therefore systematically under-counts unknown "
            "toolchains. Two commonly-missed entries (tsc --noEmit, swift test) "
            "were absent from an earlier version of this list, silently costing "
            "models credit they had genuinely earned. Extend the list via "
            "[verifiers] extra in config."
        ),
        "verifiers_matched_against": list(verifier_list(_CFG.extra_verifiers)),
    }


@mcp.tool(description="The claim-vs-verify taxonomy, its definitions, and its validation status.")
def claim_taxonomy() -> dict[str, Any]:
    return {
        "classes": CLAIM_CLASSES,
        "validated": False,
        "caveat": TAXONOMY_CAVEAT,
        "why_no_llm_judge": (
            "Deliberately not automated with an LLM judge. Adding a second "
            "unvalidated instrument to measure the first does not produce "
            "validation; it produces two unvalidated instruments and a false "
            "sense of rigour."
        ),
    }


# ---------------------------------------------------------------------------
# 3. Routing
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Query the routing table. Every entry carries an explicit status "
        "(confirmed / provisional / void / not_measured). Pass "
        "min_status='confirmed' to filter to rows validated by a human "
        "calibration read -- that currently returns ZERO rows, which is the "
        "honest answer, not a bug."
    )
)
def routing_query(
    work_class: str | None = None,
    model: str | None = None,
    min_status: str | None = None,
) -> dict[str, Any]:
    return routing.query(work_class, model, min_status)


@mcp.tool(
    description=(
        "Routing recommendation for a work class. Returns the verdict together "
        "with its status and caveats, never a bare verdict. Returns no "
        "recommendation at all for void or unmeasured rows."
    )
)
def routing_recommend(work_class: str, model: str | None = None) -> dict[str, Any]:
    return routing.recommend(work_class, model)


@mcp.tool(description="List the work classes and models the routing table covers, with statuses.")
def routing_index() -> dict[str, Any]:
    t = routing.load_table()
    return {
        "work_classes": routing.work_classes(),
        "models": routing.models(),
        "status_levels": t["status_levels"],
        "global_caveats": t["global_caveats"],
        "open_items": t["open_items"],
    }


@mcp.tool(description="Effective configuration and where it was loaded from.")
def server_config() -> dict[str, Any]:
    return _cfg_summary()


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
