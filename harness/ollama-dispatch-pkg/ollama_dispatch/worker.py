"""Standalone agentic coding worker against Ollama's native /api/chat.

This talks to an Ollama server directly and implements its own minimal
tool-execution loop. The single structural rule that keeps it reliable:

    Termination is based purely on "the model's response has no more
    tool_calls" -- never on injecting a self-generated "continue" prompt.

That is the whole loop. The model is given the tools in tools.py, and the loop
runs until the model stops calling tools (or calls task_complete), a hard
iteration cap is hit, or a wall-clock timeout fires.

Two call conventions are handled transparently: native `tool_calls`, and the
manual JSON-in-content fallback for models that cannot emit the native
tool-call token (see tools.extract_manual_tool_calls). A `--verify` command, if
given, is run at the end and its pass/fail recorded -- this is what the dispatch
pipeline uses as its convergence signal.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config, web as web_mod
from .tools import (
    TOOLS,
    dispatch_tool,
    extract_manual_tool_calls,
    render_manual_tools_block,
)

DEFAULT_MAX_ITERS = 30
DEFAULT_CHAT_TIMEOUT_S = 600
MAX_TOOL_CALLS_PER_TURN = 12  # a runaway turn can't burn the whole budget
ITERATION_LOW_BUDGET_THRESHOLD = 3

SYSTEM_PROMPT = """You are a focused coding agent working inside a single \
repository. You complete the task described in the first user message by \
calling the provided tools. Rules:
- Make the smallest change that satisfies the task.
- Read before you write. Verify your edits by reading them back or running tests.
- When the task is done, call task_complete with a short summary. Do not keep \
calling tools after the work is finished.
- Never fabricate file contents or command output; use the tools."""


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Host resolution + routing
# --------------------------------------------------------------------------

def resolve_host(host_arg: str | None, model: str) -> str:
    """Turn a --host argument into a base URL.

    - a full URL (http...) is used verbatim
    - a name present in the host table is looked up
    - 'auto' (or None) runs pick_host() over the configured host table
    """
    if host_arg and host_arg.startswith("http"):
        return host_arg
    if host_arg and host_arg != "auto":
        return config.host_url(host_arg)
    return pick_host(model)


def _get_model_size_on_host(base_url: str, model: str):
    """Ask a host's /api/tags for the model's real on-disk size, or None."""
    try:
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=5) as resp:
            data = json.loads(resp.read())
    except Exception:  # noqa: BLE001
        return None
    for m in data.get("models", []):
        if m.get("name") == model or m.get("model") == model:
            return m.get("size")
    return None


def _host_is_free(base_url: str) -> bool:
    """A host is 'free' when it currently has no model loaded (/api/ps empty)."""
    try:
        with urllib.request.urlopen(f"{base_url}/api/ps", timeout=5) as resp:
            data = json.loads(resp.read())
        return not data.get("models")
    except Exception:  # noqa: BLE001
        return True  # can't tell -> don't let it block routing


def pick_host(model: str) -> str:
    """Choose which configured Ollama host to dispatch `model` to.

    Priority: the PRIMARY host (config.BIG_HOST_NAME, else the first configured
    host) is preferred and used as the queue-there fallback. Every other host is
    overflow capacity, eligible ONLY when the model's real size provably fits
    its configured usable_bytes budget. A host with usable_bytes=None is never
    proven to fit, so auto-routing never picks it.

      * model fits no overflow budget  -> primary, regardless of busy-ness
      * primary free                   -> primary
      * primary busy, fits an overflow -> that overflow host (parallel capacity)
      * size unknown / no overflow      -> primary (queues there; safest)
    """
    hosts = config.load_hosts()
    if not hosts:
        return config.DEFAULT_HOST_URL
    primary = config.BIG_HOST_NAME if config.BIG_HOST_NAME in hosts else next(iter(hosts))
    primary_url = hosts[primary]["url"]

    known_size = None
    for spec in hosts.values():
        size = _get_model_size_on_host(spec["url"], model)
        if size:
            known_size = size
            break

    overflow = []
    if known_size is not None:
        for name, spec in hosts.items():
            if name == primary:
                continue
            ub = spec.get("usable_bytes")
            if ub is not None and known_size <= ub:
                overflow.append((name, spec["url"]))

    if known_size is not None and not overflow:
        log(f"[worker] pick_host: {model} fits no overflow budget -- {primary} only.")
        return primary_url
    if _host_is_free(primary_url):
        return primary_url
    if overflow:
        name, url = overflow[0]
        log(f"[worker] pick_host: {primary} busy -- routing {model} to overflow host {name}.")
        return url
    return primary_url


# --------------------------------------------------------------------------
# Chat transport
# --------------------------------------------------------------------------

def chat_once(base_url: str, model: str, messages: list, num_ctx: int,
              temperature: float, native_tools: bool, timeout: int) -> dict:
    """One /api/chat round. Returns the assistant message dict."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
    }
    if native_tools:
        payload["tools"] = TOOLS
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{base_url}/api/chat", data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read())
    return body.get("message", {})


def _tool_calls_from_message(msg: dict, native_tools: bool) -> list:
    """Extract tool calls from an assistant message, native or manual."""
    calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        calls.append({"name": fn.get("name", ""), "arguments": args})
    if not calls and msg.get("content"):
        calls = extract_manual_tool_calls(msg["content"])
    return calls[:MAX_TOOL_CALLS_PER_TURN]


# --------------------------------------------------------------------------
# The agentic loop
# --------------------------------------------------------------------------

def run(task: str, cwd: Path, model: str, base_url: str, *, num_ctx: int,
        temperature: float, max_iters: int, chat_timeout: int,
        native_tools: bool, verify_cmd: str | None) -> dict:
    """Run the agentic loop. Returns a result dict with convergence + verify."""
    web = web_mod
    system = SYSTEM_PROMPT
    if not native_tools:
        system += "\n\n" + render_manual_tools_block(TOOLS)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": task},
    ]

    converged = False
    summary = ""
    start = time.time()

    for iteration in range(1, max_iters + 1):
        remaining = max_iters - iteration
        if remaining <= ITERATION_LOW_BUDGET_THRESHOLD:
            messages.append({
                "role": "user",
                "content": f"[harness] {remaining} iterations left. If you are close, "
                           f"finish and call task_complete; request_more_iterations exists "
                           f"if you genuinely need more.",
            })
        try:
            msg = chat_once(base_url, model, messages, num_ctx, temperature,
                            native_tools, chat_timeout)
        except urllib.error.URLError as e:
            log(f"[worker] chat error: {e}")
            return {"converged": False, "error": str(e), "iterations": iteration}

        messages.append({"role": "assistant", "content": msg.get("content", ""),
                         **({"tool_calls": msg["tool_calls"]} if msg.get("tool_calls") else {})})
        calls = _tool_calls_from_message(msg, native_tools)

        if not calls:
            # No more tool calls -> the model is done. This is the ONLY
            # structural termination condition.
            log(f"[worker] iteration {iteration}: no tool calls -- terminating.")
            summary = msg.get("content", "")
            converged = True
            break

        done = False
        for call in calls:
            name, args = call["name"], call["arguments"]
            if name == "task_complete":
                summary = args.get("summary", "")
                converged = True
                done = True
                log(f"[worker] task_complete: {summary[:200]}")
                break
            if name == "request_more_iterations":
                extra = int(args.get("count", 5) or 5)
                max_iters += extra
                result = f"granted {extra} more iterations"
                log(f"[worker] {result} (reason: {args.get('reason', '')[:120]})")
            else:
                try:
                    result = dispatch_tool(name, cwd, args, web=web)
                except Exception as e:  # noqa: BLE001
                    result = f"ERROR running {name}: {e}"
            messages.append({"role": "tool", "content": str(result)})

        if done:
            break
        if time.time() - start > chat_timeout * max_iters:
            log("[worker] wall-clock budget exhausted.")
            break
    else:
        log(f"[worker] hit max_iters={max_iters} without task_complete.")

    verify_passed = None
    if verify_cmd:
        verify_passed = _run_verify(verify_cmd, cwd)

    return {
        "converged": converged,
        "summary": summary,
        "iterations": iteration,
        "verify_passed": verify_passed,
        "elapsed_s": round(time.time() - start, 1),
    }


def _run_verify(verify_cmd: str, cwd: Path) -> bool:
    import subprocess

    log(f"[worker] running verify: {verify_cmd}")
    try:
        proc = subprocess.run(verify_cmd, shell=True, cwd=str(cwd),
                              capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        log("[worker] verify timed out.")
        return False
    log(f"[worker] verify exit {proc.returncode}")
    if proc.stdout:
        log(proc.stdout[-2000:])
    return proc.returncode == 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Agentic Ollama coding worker.")
    ap.add_argument("--task", help="Task text, or use --task-file.")
    ap.add_argument("--task-file", help="Path to a file containing the task.")
    ap.add_argument("--cwd", default=".", help="Working directory (repo/worktree).")
    ap.add_argument("--model", default=config.DEFAULT_MODEL)
    ap.add_argument("--host", default="auto",
                    help="Host name from hosts.json, a full URL, or 'auto' to route.")
    ap.add_argument("--num-ctx", type=int, default=config.DEFAULT_NUM_CTX)
    ap.add_argument("--temperature", type=float, default=config.DEFAULT_TEMPERATURE)
    ap.add_argument("--max-iters", type=int, default=DEFAULT_MAX_ITERS)
    ap.add_argument("--chat-timeout", type=int, default=DEFAULT_CHAT_TIMEOUT_S)
    ap.add_argument("--manual-tools", action="store_true",
                    help="Force the plain-text tool-call convention (for models "
                         "that cannot emit native tool_calls tokens).")
    ap.add_argument("--verify", help="Command run after the loop; its exit code "
                                     "is the convergence signal.")
    ap.add_argument("--json", action="store_true", help="Emit the result as JSON.")
    args = ap.parse_args(argv)

    task = args.task
    if args.task_file:
        task = Path(args.task_file).read_text()
    if not task:
        ap.error("one of --task or --task-file is required")

    cwd = Path(args.cwd).expanduser().resolve()
    base_url = resolve_host(args.host, args.model)
    log(f"[worker] model={args.model} host={base_url} cwd={cwd}")

    result = run(
        task, cwd, args.model, base_url,
        num_ctx=args.num_ctx, temperature=args.temperature,
        max_iters=args.max_iters, chat_timeout=args.chat_timeout,
        native_tools=not args.manual_tools, verify_cmd=args.verify,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        log(f"[worker] done: converged={result['converged']} "
            f"verify_passed={result.get('verify_passed')} "
            f"iterations={result['iterations']}")
    ok = result["converged"] and (result.get("verify_passed") in (None, True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
