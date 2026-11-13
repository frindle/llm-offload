#!/usr/bin/env python3
"""MCP stdio server: the ONLY tool surface the Unraid-hosted Goose chat gets.

operator 2026-10-01: chat runs on Unraid (qwen3:14b, num_ctx pinned to the pregate's
UNRAID_CONFIRMED_SAFE_CTX value so Ollama never reloads), and EVERY investigation /
coding / research task is dispatched to the Studio queue instead of being done in the
chat turn. So the chat model does not need a developer/shell/edit/write/vault/ollama
tool surface at all -- it needs exactly one verb ("send this to the queue") plus two
read verbs ("what is the queue doing", "what did job X say").

WHY THE SCHEMAS ARE THIS SMALL
  Goose 1.50.0 sends every enabled tool's JSON schema on EVERY request. Measured on
  the operator's box: the default profile's 29 tool definitions were 19,235 chars (~5.5k tokens)
  of a 28,784-char request -- which does not fit a 6144-token window at all, let alone
  leave room to talk. Every word of description here is paid for on every single turn,
  so the descriptions are terse ON PURPOSE. Do not "improve" them into prose.

INVARIANT: this server launches queue jobs but never runs inference itself, and never
holds a model key. It shells out to ~/bin/ollama-queue.py, which owns the lane, the
VRAM-collision guards and the dashboard row.

Protocol: JSON-RPC 2.0 over stdio, one JSON object per line (MCP's stdio transport).
Implements initialize / notifications/initialized / tools/list / tools/call / ping.
The client's protocolVersion is echoed back, so this keeps working across MCP revisions.

Run (goose launches it; this is only for poking at it by hand):
    echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 ~/bin/goose-queue-mcp.py
"""
import os as _qc_os, sys as _qc_sys
_qc_dir = _qc_os.path.dirname(_qc_os.path.realpath(__file__))
if _qc_dir not in _qc_sys.path:
    _qc_sys.path.append(_qc_dir)
import queue_config
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

HOME = Path(os.environ.get("GOOSE_QUEUE_HOME") or Path.home())
QUEUE = HOME / "bin" / "ollama-queue.py"
STATE = queue_config.state_dir() / "ollama-queue-state.json"
SPOOL = Path(os.environ.get("GOOSE_QUEUE_SPOOL") or (HOME / ".cache" / "goose-dispatch"))

# The Darkbloom lane. Hard-coded rather than guessed per call: the operator's standing rule is
# that all dispatched work goes to the one studio-db lane, so a chat turn cannot
# accidentally aim a job at Unraid and evict the chat model it is talking to.
LANE = "studio-db"
# Any ':'-tagged legacy tag is aliased by ollama-queue.py to the Darkbloom model anyway;
# naming it here keeps the enqueue line honest about what actually runs.
MODEL = os.environ.get("DARKBLOOM_DEFAULT_MODEL", "qwen3.6-35b-a3b-vl-mtp-mxfp8")

# Every tool reply is clamped to this many chars. The chat model has ~3k tokens of room
# to work in; a 20k-char `status` dump would blow the window and force goose to
# summarise, which costs another whole turn. Short summaries are a HARD requirement of
# this design, not a nicety.
MAX_REPLY = int(os.environ.get("GOOSE_QUEUE_MCP_MAX_REPLY", "1200"))
TIMEOUT = float(os.environ.get("GOOSE_QUEUE_MCP_TIMEOUT", "180"))

SERVER_INFO = {"name": "queue", "version": "1.0.0"}
DEFAULT_PROTOCOL = "2025-06-18"

# ---------------------------------------------------------------- tool definitions
# Terse by design (see module docstring). Measured cost of this whole block as sent on
# the wire: see test-goose-queue-proxy.py::test_tool_budget, which FAILS if it grows.
TOOLS = [
    {
        "name": "queue_dispatch",
        # "FIRST action" and the explicit "do not call queue_status first" are not
        # padding: measured 11/13 correct on the first wording, and BOTH failures were
        # the model calling queue_status before dispatching anything (valid JSON, right
        # schema, wrong verb). See test-goose-queue-proxy.py's budget test for the cost.
        "description": "Send a task to the dispatch queue (a local model does it, not you). Your FIRST action for any investigation, code change, research or multi-file read -- do not call queue_status first. Returns a job id immediately; it does not wait.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Full self-contained instructions."},
                "label": {"type": "string", "description": "Short slug for the dashboard."},
                "repo": {"type": "string", "description": "Repo path, if it touches code."},
            },
            "required": ["task"],
        },
    },
    {
        "name": "queue_status",
        "description": "Summary of running/pending queue jobs. Only when asked what the queue is doing.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "queue_result",
        "description": "Outcome of a finished job, by id or label.",
        "inputSchema": {
            "type": "object",
            "properties": {"job": {"type": "string", "description": "Job id or label substring."}},
            "required": ["job"],
        },
    },
]


def log(msg):
    # stderr only: stdout is the JSON-RPC channel and a stray print corrupts the stream.
    print(f"[goose-queue-mcp] {msg}", file=sys.stderr, flush=True)


def clip(text, limit=None):
    """Clamp a reply, saying so. Truncating SILENTLY is worse than truncating: the model
    would treat a cut-off list as the complete answer."""
    limit = MAX_REPLY if limit is None else limit
    text = (text or "").strip()
    if len(text) <= limit:
        return text or "(no output)"
    return text[:limit].rstrip() + f"\n... [clipped, {len(text)} chars total]"


def _run(args):
    """Run ollama-queue.py. Returns (rc, stdout, stderr); never raises."""
    try:
        p = subprocess.run(["python3", str(QUEUE)] + list(args),
                           capture_output=True, text=True, timeout=TIMEOUT)
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return 1, "", f"ollama-queue.py timed out after {TIMEOUT}s"
    except Exception as e:
        return 1, "", f"could not run ollama-queue.py: {type(e).__name__}: {e}"


def _slug(text, n=6):
    words = "".join(c if (c.isalnum() or c in " -_") else " " for c in text or "").split()
    return "-".join(words[:n])[:48].strip("-").lower()


# ------------------------------------------------------------------------- tools
def queue_dispatch(args):
    """Enqueue ONE research job on the studio-db lane.

    task-kind research, not coding: a coding dispatch without --verify is refused by the
    queue (correctly -- it has no goal signal and thrashes to max-iters), and a chat-side
    request has no verify command to offer. Research is the honest kind for "go find out";
    if it turns into a code change, that gets dispatched properly from a real session
    with a verify command, which is a human decision, not one to fake from here."""
    task = (args.get("task") or "").strip()
    if not task:
        return "refused: `task` is empty.", True
    label = _slug(args.get("label") or task)
    job_dir = SPOOL / f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    job_dir.mkdir(parents=True, exist_ok=True)
    task_file = job_dir / "TASK.md"
    task_file.write_text(task)
    cmd = ["enqueue", "--model", MODEL, "--host", LANE,
           "--task-file", str(task_file),
           "--task-kind", "research",
           "--label", f"goose-{label}" if label else "goose-dispatch",
           "--allow-duplicate-label"]
    repo = (args.get("repo") or "").strip()
    if repo:
        # --repo gets the queue's ENFORCED per-dispatch worktree isolation. --cwd would
        # be the legacy unisolated path; never offer that from a chat tool.
        cmd += ["--repo", repo]
    else:
        cmd += ["--cwd", str(job_dir), "--allow-unisolated"]
    rc, out, err = _run(cmd)
    for line in out.splitlines():
        if line.startswith("enqueued "):
            return f"queued job {line.split()[1]} on {LANE}: {label or task[:40]}", False
    return clip(f"enqueue failed (rc={rc}): {(err or out).strip()}"), True


_ROW = re.compile(r"^\[(?P<st>[a-z_]+)\s*\]\s+(?P<id>[0-9a-f]{6,})\s+(?P<label>\S+)(?P<rest>.*)$")


def queue_status(_args):
    """Running + pending only, one short line each.

    Parsed from `status` rather than the state file so the human and the model see the
    SAME rows, and filtered to the live ones: `status` prints every retained done/failed
    row too (hundreds, on the operator's box), which is a worklist for a human at a terminal and
    pure window-burn for a 6144-token chat model."""
    rc, out, err = _run(["status"])
    if rc != 0 and not out:
        return clip(f"queue status failed: {(err or out).strip()}"), True
    live, counts = [], {}
    for line in out.splitlines():
        m = _ROW.match(line.strip())
        if not m:
            continue
        st = m.group("st")
        counts[st] = counts.get(st, 0) + 1
        if st in ("running", "pending", "paused", "held", "scheduled"):
            live.append(f"{st:8} {m.group('id')[:12]} {m.group('label')[:40]}")
    head = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no jobs"
    if not live:
        return clip(f"{head}\nnothing running or pending."), False
    return clip(head + "\n" + "\n".join(live)), False


def queue_result(args):
    """One finished job's outcome: status/verdict plus the head of its answer."""
    key = (args.get("job") or "").strip()
    if not key:
        return "refused: `job` is empty.", True
    rc, out, err = _run(["results", "--json", "--limit", "200"])
    try:
        rows = json.loads(out)
        if not isinstance(rows, list):
            raise ValueError("results --json did not return a list")
    except Exception as e:
        return clip(f"could not read results (rc={rc}): {e} {(err or '').strip()}"), True
    low = key.lower()
    hit = next((r for r in rows if str(r.get("id") or "").startswith(key)), None)
    if hit is None:
        hit = next((r for r in rows if low in str(r.get("label") or "").lower()), None)
    if hit is None:
        return clip(f"no finished job matches {key!r}. It may still be running "
                    f"(queue_status)."), False
    bits = [f"{hit.get('id')} {hit.get('label')}",
            f"status={hit.get('status')} verdict={hit.get('verdict') or '-'} "
            f"exit={hit.get('exit_code')}"]
    if hit.get("failure_detail"):
        bits.append(f"failure: {hit['failure_detail']}")
    answer = hit.get("answer")
    if not answer and hit.get("answer_path"):
        try:
            answer = Path(hit["answer_path"]).read_text()
        except OSError:
            answer = None
    if answer:
        bits.append("answer:\n" + str(answer))
    return clip("\n".join(bits)), False


HANDLERS = {"queue_dispatch": queue_dispatch, "queue_status": queue_status,
            "queue_result": queue_result}


# -------------------------------------------------------------------- JSON-RPC
def _result(rpc_id, payload):
    return {"jsonrpc": "2.0", "id": rpc_id, "result": payload}


def _error(rpc_id, code, message):
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def handle(req):
    """One request -> one response dict, or None for a notification (no id => no reply;
    sending one for `notifications/initialized` is a protocol violation some clients
    log as an error)."""
    method = req.get("method")
    rpc_id = req.get("id")
    params = req.get("params") or {}
    if rpc_id is None:
        return None
    if method == "initialize":
        return _result(rpc_id, {
            "protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    if method == "ping":
        return _result(rpc_id, {})
    if method == "tools/list":
        return _result(rpc_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        fn = HANDLERS.get(name)
        if fn is None:
            return _error(rpc_id, -32602, f"unknown tool {name!r}")
        try:
            text, is_error = fn(params.get("arguments") or {})
        except Exception as e:  # a tool crash must be an isError result, not a dead server
            log(f"tool {name} crashed: {type(e).__name__}: {e}")
            text, is_error = f"{name} failed: {type(e).__name__}: {e}", True
        return _result(rpc_id, {"content": [{"type": "text", "text": text}],
                                "isError": bool(is_error)})
    return _error(rpc_id, -32601, f"method not found: {method}")


def main(argv=None):
    SPOOL.mkdir(parents=True, exist_ok=True)
    log(f"ready; queue={QUEUE} lane={LANE} tools={[t['name'] for t in TOOLS]}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            print(json.dumps(_error(None, -32700, f"parse error: {e}")), flush=True)
            continue
        resp = handle(req)
        if resp is not None:
            print(json.dumps(resp), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
