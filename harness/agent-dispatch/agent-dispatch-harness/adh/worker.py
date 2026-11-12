"""The agentic worker: one model, one task, one working tree, one transcript.

The worker's only job is to run the agent loop faithfully and to record what
happened in enough detail that scoring never has to guess. Everything
judgemental — did it succeed, was its claim true — happens later, from the
transcript, against ground truth the worker never sees.

STOP REASONS
------------
Recorded per run, most specific first, because they demand different responses:

  converged       the model stopped on its own with a final summary
  iter_cap        hit the iteration ceiling — a budget question
  config_ceiling  filled the CONFIGURED context window; the knob can be turned
  native_ceiling  filled the model's OWN maximum window; nothing to turn
  timeout         hit the wall clock — an instrument defect to investigate
  load_failed     never got a usable response from the backend

The config/native split is the whole point of recording both windows. One is a
choice we made and can revisit next round; the other is a property of the model
and a routing fact. Collapsing them loses the difference between "give it more
room" and "this model cannot hold this task".
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import tools as T


@dataclass
class RunOutcome:
    converged: bool = False
    iterations: int = 0
    stop_reason: str = "none"
    prompt_tokens: int = -1
    output_tokens: int = -1
    decode_s: float = -1.0
    warmup_s: float = 0.0
    loop_s: float = 0.0
    timed_out: bool = False
    final_message: str = ""
    messages: list = field(default_factory=list)
    error: str = ""

    @property
    def out_tps(self) -> float:
        if self.output_tokens > 0 and self.decode_s > 0:
            return round(self.output_tokens / self.decode_s, 1)
        return -1.0


class BackendError(RuntimeError):
    pass


def _post(url: str, payload: dict, timeout: float) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise BackendError(f"HTTP {e.code} from {url}: {e.read()[:400]!r}") from e
    except Exception as e:                                           # noqa: BLE001
        raise BackendError(f"{type(e).__name__} calling {url}: {e}") from e


def probe_native_ctx(host: str, model: str) -> int:
    """Ask the backend for the model's own maximum context.

    Worth doing rather than trusting a table: a config that runs a model past
    its native window is not producing a bigger window, it is producing an
    out-of-spec run, and the results row would look ordinary.
    """
    try:
        info = _post(host.rstrip("/") + "/api/show", {"model": model}, 60)
    except BackendError:
        return 0
    for key, val in (info.get("model_info") or {}).items():
        if key.endswith(".context_length"):
            try:
                return int(val)
            except (TypeError, ValueError):
                return 0
    return 0


class Worker:
    def __init__(self, *, host: str, model: str, num_ctx: int,
                 temperature: float = 0.2, manual_tools: bool = False,
                 api_style: str = "ollama", max_iterations: int = 25,
                 read_max_chars: int = 60000, bash_timeout_s: int = 180,
                 repeat_nudge_after: int = 2, web: dict | None = None,
                 log=print):
        self.host = host.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.temperature = temperature
        self.manual_tools = manual_tools
        self.api_style = api_style
        self.max_iterations = max_iterations
        self.read_max_chars = read_max_chars
        self.bash_timeout_s = bash_timeout_s
        self.repeat_nudge_after = repeat_nudge_after
        self.web = web or {"enabled": False}
        self.log = log
        self.tool_schemas = T.tool_schemas(bool(self.web.get("enabled")))

    # -- backend -------------------------------------------------------------
    def _chat(self, messages: list, use_tools: bool, timeout: float) -> dict:
        if self.api_style == "openai":
            url = self.host + "/v1/chat/completions"
            payload = {"model": self.model, "messages": messages, "stream": False,
                       "temperature": self.temperature}
            if use_tools:
                payload["tools"] = self.tool_schemas
            raw = _post(url, payload, timeout)
            choice = (raw.get("choices") or [{}])[0]
            usage = raw.get("usage") or {}
            return {"message": choice.get("message") or {},
                    "prompt_eval_count": usage.get("prompt_tokens", -1),
                    "eval_count": usage.get("completion_tokens", -1),
                    # The OpenAI-compatible shape carries no generation
                    # duration. Reported as -1 (unavailable), never as 0, so a
                    # missing measurement cannot be read as instant generation.
                    "eval_duration": -1}
        url = self.host + "/api/chat"
        payload = {"model": self.model, "messages": messages, "stream": False,
                   "options": {"temperature": self.temperature,
                               "num_ctx": self.num_ctx}}
        if use_tools:
            payload["tools"] = self.tool_schemas
        return _post(url, payload, timeout)

    def warmup(self, timeout: float = 900) -> float:
        """Load the model before the clock that measures the loop starts.

        Load time is a property of the host and the file layout, not of the
        model's reasoning. Folding it into loop time would make a cold start
        look like slow thinking.
        """
        t0 = time.time()
        self._chat([{"role": "user", "content": "ready"}], use_tools=False,
                   timeout=timeout)
        return round(time.time() - t0, 1)

    # -- the loop ------------------------------------------------------------
    def run(self, cwd: Path, task: str, wall_s: float) -> RunOutcome:
        box = T.Toolbox(cwd, read_max_chars=self.read_max_chars,
                        bash_timeout_s=self.bash_timeout_s, web=self.web)
        out = RunOutcome()

        system = T.SYSTEM_PROMPT
        if self.manual_tools:
            system += "\n\n" + T.render_manual_tools_block(self.tool_schemas)
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": task}]
        out.messages = messages

        try:
            out.warmup_s = self.warmup()
        except BackendError as e:
            out.stop_reason = "load_failed"
            out.error = str(e)
            return out

        started = time.time()
        repeats: dict[str, int] = defaultdict(int)
        i = 0
        for i in range(1, self.max_iterations + 1):
            remaining = wall_s - (time.time() - started)
            if remaining <= 0:
                out.timed_out = True
                out.stop_reason = "timeout"
                break

            self.log(f"[worker] --- iteration {i}/{self.max_iterations} ---")
            try:
                resp = self._chat(messages, use_tools=not self.manual_tools,
                                  timeout=min(remaining, wall_s))
            except BackendError as e:
                out.error = str(e)
                out.stop_reason = "load_failed" if i == 1 else "backend_error"
                break

            msg = resp.get("message") or {}
            content = msg.get("content") or ""
            pt, ot = resp.get("prompt_eval_count", -1), resp.get("eval_count", -1)
            if isinstance(pt, int) and pt >= 0:
                out.prompt_tokens = pt
            if isinstance(ot, int) and ot >= 0:
                out.output_tokens = (max(out.output_tokens, 0) + ot)
            dur = resp.get("eval_duration", -1)
            if isinstance(dur, (int, float)) and dur and dur > 0:
                out.decode_s = round(max(out.decode_s, 0.0) + dur / 1e9, 1)

            # CONTEXT CEILING. Detected as the prompt filling the window rather
            # than as a thrown error, because backends silently drop the oldest
            # turns instead of failing: the run keeps going, having forgotten
            # the task, and terminates later looking like confusion.
            if out.prompt_tokens > 0 and out.prompt_tokens >= self.num_ctx * 0.97:
                out.stop_reason = "context_ceiling"
                messages.append({"role": "assistant", "content": content})
                break

            calls = list(msg.get("tool_calls") or [])
            manual: list[dict] = []
            if not calls and content.strip():
                manual = T.extract_manual_tool_calls(content)

            messages.append({"role": "assistant", "content": content,
                             **({"tool_calls": calls} if calls else {})})

            if not calls and not manual:
                # A plain-text reply with no calls is the model saying it is
                # done. That is the ONLY thing "converged" means here — it is
                # not a success claim, and nothing about the task's correctness
                # has been checked at this point.
                out.converged = True
                out.stop_reason = "converged"
                out.final_message = content
                break

            nudges: list[str] = []
            invocations = ([( (c.get("function") or {}).get("name"),
                              (c.get("function") or {}).get("arguments"),
                              c.get("id")) for c in calls]
                           if calls else
                           [(c.get("name"), c.get("arguments"), None) for c in manual])

            for name, args, call_id in invocations:
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:                                # noqa: BLE001
                        args = {}
                args = args or {}
                result = box.dispatch(name or "", args)

                sig = f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
                repeats[sig] += 1
                if repeats[sig] > self.repeat_nudge_after:
                    nudges.append(
                        f"You have now called {name} with identical arguments "
                        f"{repeats[sig]} times and the result has not changed. It is "
                        f"already in this conversation above. Do something different.")

                if manual:
                    # A tool-role message combined with omitting the native
                    # `tools` field is an untested combination for the models
                    # that need manual tools at all; a plain user-role result
                    # is what works end to end for them.
                    messages.append({"role": "user",
                                     "content": f"[tool result for {name}]: {result}"})
                elif self.api_style == "openai":
                    m = {"role": "tool", "content": str(result)}
                    if call_id:
                        m["tool_call_id"] = call_id
                    messages.append(m)
                else:
                    messages.append({"role": "tool", "content": str(result)})

            if nudges:
                messages.append({"role": "user", "content": "\n\n".join(nudges)})

        else:
            out.stop_reason = "iter_cap"

        out.iterations = i
        out.loop_s = round(time.time() - started, 1)
        out.messages = messages
        return out


def write_transcript(path: Path, *, model: str, task: str, cwd: Path,
                     outcome: RunOutcome) -> Path:
    """Archive the full message list.

    Transcripts are the ground truth every post-hoc scorer reads, and they are
    also verbatim source from whatever repository the task ran against — which
    is why the shipped .gitignore excludes them. Publish results; do not
    publish transcripts unless the target repository is yours to publish.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "model": model, "task": task, "cwd": str(cwd),
        "converged": outcome.converged, "iterations": outcome.iterations,
        "stop_reason": outcome.stop_reason,
        "messages": outcome.messages,
    }, indent=2))
    return path
