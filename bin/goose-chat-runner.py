#!/usr/bin/env python3
"""Queue RUNNER that serves ONE Goose (or any OpenAI-client) chat completion.

WHY THIS EXISTS (the owner 2026-10-01: "goose needs to run through queue for everything,
even chat"): standing policy is that ALL GPU/inference work goes through
~/bin/ollama-queue.py, so the single studio-db lane (local Darkbloom, cap shared with
fleet traffic) is honestly accounted for and visible on the dashboard. Until now Goose
talked to Darkbloom's OpenAI endpoint DIRECTLY, taking a slot invisibly. Now
~/bin/goose-queue-proxy.py enqueues one queue job per chat request and this runner is
the thing the queue launches on the lane.

CONTRACT WITH THE QUEUE (ALLOWED_RUNNERS, ollama-queue.py): the queue passes exactly
--model --host --num-ctx --cwd --task-file and nothing else; the runner owns everything
else and its own output. `--task-file` is the VERBATIM OpenAI request body (JSON) the
client sent; `--cwd` is the per-request job directory the proxy created, and is where
this runner writes its output files:

    request.json   (= --task-file; the proxy writes it)
    stream.sse     SSE bytes, appended + flushed as they arrive upstream (stream:true)
    response.json  the upstream JSON response (stream:false)
    error.json     an OpenAI-shaped error object, on any failure
    done.json      TERMINAL marker, written LAST: {"ok": bool, "stream": bool, ...}

The proxy tails stream.sse and relays it to the client; `done.json` is what tells it the
job is over. Nothing here is a code dispatch: the job is enqueued --task-kind research
with a --runner, which ollama-queue.py's `ungateable()` already treats as having no diff
to gate, so no gate/regate row is ever created for a chat turn.

DIFFERENCE FROM ~/bin/darkbloom_chat.py: that module is the non-streaming,
JSON-schema-oriented single-shot path the review/research runners use -- it builds its
own body (system+user messages, response_format ladder, enable_thinking=false) and
strips <think> blocks. A chat proxy must do the OPPOSITE: forward the client's body
MOSTLY VERBATIM, including `tools`, `tool_choice`, `stream: true`, `temperature`,
`response_format` and whatever else Goose sends, because the client -- not us -- owns the
protocol. So only its AUTH/ALIAS/TRANSIENT-RETRY facts are reused (imported, not copied):

  * the Bearer key lives in ~/.darkbloom/local.json and ROTATES on every Darkbloom
    restart, so it is re-read per request (and per retry) and NEVER logged, printed or
    put in argv;
  * the base URL comes from the same record (darkbloom_chat.base_url());
  * 429/500/502/503/504 are transient (shared slots at the cap, a provider restart) and
    are re-sent after a short backoff -- but ONLY while nothing has been relayed yet: a
    retry after the client has already seen tokens would duplicate the response.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import darkbloom_chat as _dbk  # auth/alias/transient-retry facts (see module docstring)

# Output filenames -- the wire protocol between this runner and goose-queue-proxy.py.
REQUEST_FILE = "request.json"
STREAM_FILE = "stream.sse"
RESPONSE_FILE = "response.json"
ERROR_FILE = "error.json"
DONE_FILE = "done.json"

# Whole-request ceiling for one chat turn. Generous: an interactive turn is seconds, but
# a long agent turn on a loaded box is not, and the proxy's keep-alives mean the CLIENT
# is not the thing that times out.
TIMEOUT = int(os.environ.get("GOOSE_QUEUE_UPSTREAM_TIMEOUT", "1800"))
RETRY_CODES = (429, 500, 502, 503, 504)
RETRIES = 3
# Request headers we must NOT forward: auth is ours to set (the rotating Darkbloom key),
# and hop-by-hop / body-framing headers are recomputed by urllib.
_CHUNK = 8192


def _err_obj(message, etype="upstream_error", code=None, status=502):
    """An OpenAI-shaped error envelope. The proxy relays `error` to the client verbatim,
    so the client sees a real error instead of a hang (or an empty 200)."""
    return {"error": {"message": message, "type": etype, "param": None, "code": code},
            "_status": status}


def _request(body):
    """A prepared urllib Request with a FRESHLY read Bearer key. Built per attempt, never
    cached: Darkbloom rewrites the key on every provider restart."""
    base = _dbk.base_url()
    if not base:
        raise RuntimeError("darkbloom endpoint not configured (~/.darkbloom/local.json)")
    return urllib.request.Request(
        base + "/v1/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "text/event-stream" if body.get("stream") else "application/json",
                 "Authorization": "Bearer " + str(_dbk._record().get("api_key") or "")})


def prepare_body(raw, fallback_model):
    """The client's body, forwarded VERBATIM except for the model id.

    Verbatim matters: `tools`/`tool_choice` must survive untouched or Goose's tool loop
    breaks, and so must stream/temperature/max_tokens/response_format/anything newer
    than this file. The ONE rewrite is the model: a legacy Ollama-style tag (contains
    ':') is aliased to the Darkbloom default exactly as ollama-queue.py and
    darkbloom_chat do, so an old GOOSE_MODEL value keeps working."""
    body = dict(raw)
    body["model"] = _dbk.alias_model(body.get("model") or fallback_model)
    return body


def _write_done(out, ok, **extra):
    """Write the TERMINAL marker last, atomically (tmp+replace). The proxy treats
    done.json's existence as 'the job is over', so a half-written one would make it
    report a truncated turn as complete."""
    rec = {"ok": bool(ok), "at": time.time()}
    rec.update(extra)
    tmp = out / (DONE_FILE + ".tmp")
    tmp.write_text(json.dumps(rec))
    tmp.replace(out / DONE_FILE)


def run(out, body):
    """Serve one request into `out`. Returns a process exit code."""
    stream = bool(body.get("stream"))
    sse = out / STREAM_FILE
    written = 0
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(_request(body), timeout=TIMEOUT) as resp:
                if not stream:
                    data = resp.read()
                    (out / RESPONSE_FILE).write_bytes(data)
                    _write_done(out, True, stream=False, bytes=len(data))
                    return 0
                # STREAMING: append every byte as it arrives and flush+fsync-free
                # flush so the proxy's tail sees it immediately. Bytes are copied
                # VERBATIM -- tool_call deltas, usage chunks, `data: [DONE]`, any
                # future field -- because re-serialising SSE is how passthrough
                # proxies silently corrupt tool calls.
                with open(sse, "ab", buffering=0) as fh:
                    while True:
                        chunk = resp.read1(_CHUNK) if hasattr(resp, "read1") else resp.read(_CHUNK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        written += len(chunk)
                _write_done(out, True, stream=True, bytes=written)
                return 0
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            # Transient AND nothing relayed yet -> safe to re-send with a fresh key.
            # Once any byte has reached the client a retry would duplicate output.
            if e.code in RETRY_CODES and attempt < RETRIES - 1 and written == 0:
                print(f"[goose-chat-runner] upstream {e.code}, retrying "
                      f"({attempt + 1}/{RETRIES - 1})", file=sys.stderr, flush=True)
                time.sleep(2 * (attempt + 1))
                continue
            obj = _err_obj(f"darkbloom HTTP {e.code}: {detail}", code=e.code,
                           status=e.code if 400 <= e.code < 600 else 502)
            (out / ERROR_FILE).write_text(json.dumps(obj))
            _write_done(out, False, stream=stream, bytes=written, http=e.code)
            return 1
        except Exception as e:  # transport, timeout, unconfigured endpoint, disk
            if written == 0 and attempt < RETRIES - 1 and isinstance(
                    e, (urllib.error.URLError, TimeoutError, ConnectionError)):
                time.sleep(2 * (attempt + 1))
                continue
            obj = _err_obj(f"darkbloom unreachable: {type(e).__name__}: {e}",
                           etype="connection_error", status=502)
            (out / ERROR_FILE).write_text(json.dumps(obj))
            _write_done(out, False, stream=stream, bytes=written)
            return 1
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    # EXACTLY the queue's 5-flag runner contract. --num-ctx/--host are accepted and
    # recorded but not used on the wire: Darkbloom serves its own context window and
    # its base URL comes from ~/.darkbloom/local.json (the queue's host string for this
    # lane is that same endpoint).
    ap.add_argument("--model", default=None)
    ap.add_argument("--host", default=None)
    ap.add_argument("--num-ctx", type=int, default=None)
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--task-file", required=True)
    args = ap.parse_args(argv)

    out = Path(args.cwd)
    out.mkdir(parents=True, exist_ok=True)
    try:
        raw = json.loads(Path(args.task_file).read_text())
        if not isinstance(raw, dict):
            raise ValueError("request body is not a JSON object")
    except Exception as e:
        obj = _err_obj(f"unreadable request body: {e}", etype="invalid_request_error",
                       status=400)
        (out / ERROR_FILE).write_text(json.dumps(obj))
        _write_done(out, False)
        return 1
    body = prepare_body(raw, args.model)
    print(f"[goose-chat-runner] model={body['model']} stream={bool(body.get('stream'))} "
          f"msgs={len(body.get('messages') or [])} tools={len(body.get('tools') or [])}",
          flush=True)
    rc = run(out, body)
    print(f"[goose-chat-runner] done rc={rc}", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
