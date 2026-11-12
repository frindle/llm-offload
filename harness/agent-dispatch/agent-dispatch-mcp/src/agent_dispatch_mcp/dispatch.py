"""Gated dispatch: refuse, or wait, but never thrash.

Scope note. This module does ONE model call and then verifies the claim. It is
deliberately not an agentic tool-execution loop -- that belongs in whatever
harness you already run, and this server is designed to sit in front of such a
harness rather than replace it. What it adds over a raw inference call is the
part raw calls skip: the host is checked before the call, and the model's claim
is checked after it.

The three dispositions, and why refusing is a feature:

  ``refused``  the host cannot fit this model at this context, and waiting would
               not help within the timeout. Dispatching anyway produces a run
               that pages, and a paging run's timings are noise -- worse than no
               measurement, because it looks like a measurement.
  ``waited``   the host was short but recovered inside the timeout. Recorded, so
               the wait is visible rather than showing up as an unexplained
               slow run.
  ``proceeded`` host was ready at first check.

``force`` exists because sometimes you genuinely want the run more than you want
the clean measurement. It never silently suppresses the gate: the result still
carries ``host_ready=false`` and the shortfall, so a downstream consumer can
exclude that run from any timing comparison.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .config import Config
from .gating import GateResult, check_ready, wait_for_ready
from .telemetry import HostTelemetry
from .verify import behaviour_from_transcript, classify_claim, run_verifier, verifier_list


def _post_json(url: str, payload: dict, timeout_s: int) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as r:  # noqa: S310
        return json.loads(r.read().decode())


def call_model(cfg: Config, model: str, prompt: str, num_ctx: int, system: str | None) -> dict:
    """One chat completion against an Ollama-compatible /api/chat endpoint."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"num_ctx": num_ctx},
    }
    started = time.monotonic()
    body = _post_json(
        cfg.dispatch.endpoint.rstrip("/") + "/api/chat",
        payload,
        cfg.dispatch.request_timeout_s,
    )
    return {
        "content": ((body.get("message") or {}).get("content")) or "",
        "duration_s": round(time.monotonic() - started, 2),
        "eval_count": body.get("eval_count"),
        "prompt_eval_count": body.get("prompt_eval_count"),
        "done_reason": body.get("done_reason"),
    }


def dispatch(
    cfg: Config,
    tel: HostTelemetry,
    model: str,
    prompt: str,
    num_ctx: int | None = None,
    system: str | None = None,
    verifier_command: str | None = None,
    workspace: str | None = None,
    wait: bool = True,
    force: bool = False,
    diff_text: str | None = None,
    files_changed: int | None = None,
) -> dict:
    num_ctx = num_ctx or cfg.dispatch.default_num_ctx
    workspace = workspace or cfg.dispatch.workspace

    gate: GateResult = (
        wait_for_ready(cfg, tel, model, num_ctx) if wait else check_ready(cfg, tel, model, num_ctx)
    )
    disposition = "proceeded" if gate.polls == 1 and gate.ready else (
        "waited" if gate.ready else "refused"
    )

    if not gate.ready and not force:
        return {
            "dispatched": False,
            "disposition": "refused",
            "host_ready": False,
            "gate": gate.to_dict(),
            "reason": (
                f"REFUSED: {gate.reason}. Dispatching now would page, and a paging "
                "run's timings are noise that looks like data. Free memory, lower "
                "num_ctx (weak lever -- the weights term dominates), pick a smaller "
                "model, or pass force=true and accept host_ready=false on the record."
            ),
        }

    try:
        call = call_model(cfg, model, prompt, num_ctx, system)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        return {
            "dispatched": False,
            "disposition": disposition,
            "host_ready": gate.ready,
            "gate": gate.to_dict(),
            "error": f"model call failed: {type(e).__name__}: {e}",
            "endpoint": cfg.dispatch.endpoint,
        }

    ver = run_verifier(verifier_command, workspace)
    assessment = classify_claim(
        call["content"], ver, files_changed=files_changed, stopped_early=False
    )
    behaviour = behaviour_from_transcript(
        [{"role": "assistant", "content": call["content"]}],
        verifiers=verifier_list(cfg.extra_verifiers),
        diff_text=diff_text,
    )

    return {
        "dispatched": True,
        "disposition": disposition,
        # False here means the run happened on a degraded host. Any timing in
        # this result is not comparable with a host_ready=true run.
        "host_ready": gate.ready,
        "forced": bool(force and not gate.ready),
        "gate": gate.to_dict(),
        "model": model,
        "num_ctx": num_ctx,
        "response": call,
        "verifier": ver.to_dict(),
        "claim_assessment": assessment.to_dict(),
        "behaviour": behaviour.to_dict(),
        "claim_held": assessment.claim_class == "honest_success",
        "warning": (
            None
            if gate.ready
            else "host_ready=false: this run was dispatched onto a degraded host. "
            "Exclude its timings from any comparison."
        ),
    }
