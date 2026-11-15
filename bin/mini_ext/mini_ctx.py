"""mini_ctx -- context control for the local mini-swe-agent runs (model_class: mini_ctx.CtxLitellmModel).

Measured on 264 steps of the local Qwen runs: prefill is ~75% of wall time and nothing is prefix-cached, so every extra
prompt token is paid again on each turn; ~half of the context is old thinking that arrives inside `content` and is resent
every turn. This subclass rewrites ONLY the API-bound copy of the messages (_prepare_messages_for_api); the trajectory
mini writes to disk keeps the full history.

  strip_old_thinking (default OFF: the model card wants preserve_thinking, so the operator must approve): drop the text up to
      `</think>` from every assistant turn but the last.
  shrink_obs (default ON): tool outputs older than the last `keep_whole` (6) become their first `shrink_chars` (300)
      chars plus a "rerun to see" note. The latest test-runner output is always kept whole.
  ceiling (default ON, 32000 tokens, chars/3.2): over it, only the last `ceiling_keep` (3) tool outputs stay whole
      (the latest test run still does); if still over, the oldest assistant texts are cut to a stub, then any tool output over
      8000 chars is cut to head 4000 + tail 4000.
  stable_prefix (default OFF): prefix-cache friendly shrinking, for a server that reuses the KV cache of an identical prompt
      prefix (see docs/mini-swe-agent.md, "Prefix caching"). The default mode shrinks per turn, so one more old message changes
      EVERY turn and the cached prefix dies at that message. With stable_prefix the old tool outputs (and, if strip_old_thinking is
      on, old assistant thinking) are shrunk in BLOCKS of `shrink_block` (6): the shrunk set only grows when a whole block has
      fallen behind the keep_whole window, and a message already sent is never rewritten afterwards, so between block steps
      every turn extends the previous prompt byte for byte. The only retroactive change left is a superseded test run (once a
      newer test run exists the older one is shrunk) and the ceiling safety net (emergency, rewrites older messages when hit).
The system prompt and the task (first two messages) are never touched.
Enable via the overlay: `model: {model_class: mini_ctx.CtxLitellmModel, strip_old_thinking: true}`; the runner's LAUNCH stub puts
this directory on sys.path (not PYTHONPATH: the agent's shell inherits that).
"""
import hashlib
import json
import os
import re
import sys
import threading
import time
import types
from pathlib import Path

from minisweagent.models.litellm_model import LitellmModel, LitellmModelConfig

DIR = Path(__file__).resolve().parent
CHARS_PER_TOKEN = 3.2
TESTRUN_RE = re.compile(r"TAP version|^\s*# (?:tests|pass|fail) |ℹ (?:tests|pass|fail)|test session starts|\d+ (?:passed|failed)|"
                        r"^\s*not ok \d+|AssertionError|ERR_ASSERTION|output_summary|^Ran \d+ tests?", re.M)
VIEWERS = {"cat", "grep", "egrep", "rg", "sed", "head", "tail", "nl", "less", "more", "awk", "ls", "echo", "find", "printf", "wc", "diff", "bat", "tee"}
NOTE = "\n[... {n} more chars elided from this older output; rerun the command to see it]"


class CtxConfig(LitellmModelConfig):
    strip_old_thinking: bool = False
    shrink_obs: bool = True
    keep_whole: int = 6
    shrink_chars: int = 300
    ceiling: bool = True
    ceiling_tokens: int = 32000
    ceiling_keep: int = 3
    stable_prefix: bool = False
    shrink_block: int = 6


def _text(m):
    c = m.get("content")
    if isinstance(c, list):
        c = "\n".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c if isinstance(c, str) else ""


def estimate_tokens(messages):
    n = 0
    for m in messages:
        n += len(_text(m))
        for tc in m.get("tool_calls") or []:
            n += len(str((tc.get("function") or {}).get("arguments") or ""))
    return int(n / CHARS_PER_TOKEN)


def _set_text(m, s):
    m["content"] = s


def _stub(t, n=200):
    """First n chars of an assistant text; never leaves a dangling <think>: a text with a closed think block keeps the reply after it
    (the thinking is what we are dropping), one cut inside a think block gets the closing tag."""
    if "</think>" in t:
        t = t.split("</think>", 1)[1].lstrip("\n") or t
    c = t[:n]
    return c + "</think>" if "<think>" in c and "</think>" not in c else c


def _command_of(msgs, i):
    """The bash command of the assistant tool call that produced tool message i (or '')."""
    tid = msgs[i].get("tool_call_id")
    for m in reversed(msgs[:i]):
        for tc in m.get("tool_calls") or []:
            if tc.get("id") == tid:
                try:
                    return str(json.loads((tc.get("function") or {}).get("arguments") or "{}").get("command") or "")
                except (ValueError, AttributeError):
                    return ""
    return ""


def is_test_run(msgs, i):
    """A REAL test run: the output has test-runner markers and the command is not a viewer (grep/cat/sed/... of a log or test file)."""
    if not TESTRUN_RE.search(_text(msgs[i])):
        return False
    cmd = re.sub(r"^\s*(?:cd\s+\S+\s*(?:&&|;)\s*)+", "", _command_of(msgs, i)).split()
    return not (cmd and cmd[0] in VIEWERS)


def latest_test_run(msgs):
    return next((i for i in range(len(msgs) - 1, -1, -1) if msgs[i].get("role") == "tool" and is_test_run(msgs, i)), None)


def strip_thinking(msgs):
    last = max((i for i, m in enumerate(msgs) if m.get("role") == "assistant"), default=-1)
    for i, m in enumerate(msgs):
        t = _text(m)
        if m.get("role") == "assistant" and i != last and "</think>" in t:
            _set_text(m, t.split("</think>", 1)[1].lstrip("\n"))


def shrink_tools(msgs, keep_whole, chars):
    """Shrink tool outputs except the last `keep_whole` and the latest test run."""
    tools = [i for i, m in enumerate(msgs) if m.get("role") == "tool"]
    protect = set(tools[-keep_whole:]) if keep_whole > 0 else set()
    lt = latest_test_run(msgs)
    if lt is not None:
        protect.add(lt)
    for i in tools:
        t = _text(msgs[i])
        if i not in protect and len(t) > chars + 80:
            _set_text(msgs[i], t[:chars] + NOTE.format(n=len(t) - chars))


def _block_edge(count, keep_whole, block):
    """How many of the oldest items (of `count`) may be shrunk: whole blocks only, outside the keep_whole window. Monotone in count."""
    return max(0, (count - max(keep_whole, 0)) // max(block, 1) * max(block, 1))


def shrink_tools_stable(msgs, keep_whole, chars, block):
    """Like shrink_tools but the shrunk set is the first _block_edge(...) tool outputs, so it grows in steps and earlier
    messages stay byte-identical between steps. The latest test run is still kept whole."""
    tools = [i for i, m in enumerate(msgs) if m.get("role") == "tool"]
    edge = _block_edge(len(tools), keep_whole, block)
    lt = latest_test_run(msgs)
    for i in tools[:edge]:
        t = _text(msgs[i])
        if i != lt and len(t) > chars + 80:
            _set_text(msgs[i], t[:chars] + NOTE.format(n=len(t) - chars))


def strip_thinking_stable(msgs, keep_whole, block):
    asst = [i for i, m in enumerate(msgs) if m.get("role") == "assistant"]
    for i in asst[:_block_edge(len(asst), keep_whole, block)]:
        t = _text(msgs[i])
        if "</think>" in t:
            _set_text(msgs[i], t.split("</think>", 1)[1].lstrip("\n"))


def shrink_messages(msgs, cfg):
    """Return a rewritten copy of the API-bound messages (the originals are never mutated)."""
    msgs = [dict(m) for m in msgs]
    if cfg.strip_old_thinking:
        strip_thinking_stable(msgs, 1, cfg.shrink_block) if cfg.stable_prefix else strip_thinking(msgs)
    if cfg.shrink_obs:
        if cfg.stable_prefix:
            shrink_tools_stable(msgs, cfg.keep_whole, cfg.shrink_chars, cfg.shrink_block)
        else:
            shrink_tools(msgs, cfg.keep_whole, cfg.shrink_chars)
    if cfg.ceiling and estimate_tokens(msgs) > cfg.ceiling_tokens:
        shrink_tools(msgs, cfg.ceiling_keep, cfg.shrink_chars)
        asst = [i for i, m in enumerate(msgs) if m.get("role") == "assistant"]
        for i in asst[:-cfg.ceiling_keep]:  # still over: stub out the oldest assistant texts (tool calls stay)
            if estimate_tokens(msgs) <= cfg.ceiling_tokens:
                break
            t = _text(msgs[i])
            if len(t) > 200:
                st = _stub(t)
                _set_text(msgs[i], st + NOTE.format(n=len(t) - len(st)))
        for i, m in enumerate(msgs[2:], 2):  # last resort, only when whole outputs alone bust the ceiling: cap each at 8000 chars
            t = _text(m)
            if estimate_tokens(msgs) > cfg.ceiling_tokens and m.get("role") == "tool" and len(t) > 8000:
                _set_text(m, t[:4000] + NOTE.format(n=len(t) - 8000) + t[-4000:])
    return msgs


# DRAFT NUDGE (2026-11-05, real-issue bench: sqlmeta r1 and loguru r1 phase A, cfnlint r1 and sqlmeta r2 diagnose): runs read
# code for their whole budget and never wrote the deliverable (phase A's test file, diagnose's DIAGNOSIS.md), although the
# prompt said to draft it by step N; by then that line was 20-70 turns back. When the runner sets MINI_DRAFT_FILE (an absolute
# path), the API-bound copy of the latest tool output gets one "[runner]" line once the run has taken MINI_DRAFT_STEP steps and
# the file still does not count as written (missing, empty, or still holding MINI_DRAFT_MARK, the skeleton's placeholder), and
# again every MINI_DRAFT_EVERY steps after that (0: once). The line says how far the run is (step k of MINI_DRAFT_CAP), what is
# wrong with the file and MINI_DRAFT_SAY (the stage's own instruction). A file named in MINI_DRAFT_ALT (comma-separated: the
# diagnose stage's NEEDS.md) that has text counts as written too. MINI_DRAFT_HEADS ('|'-separated heading names: the diagnose
# report's six) makes a file that lacks one of them (no `## <name>` line outside a code fence) count as not written either:
# paramr2 f4f87714860e built DIAGNOSIS.md quote by quote under ## Code path in all 3 attempts and never wrote the other five,
# and a nudge that only asked whether the file existed never fired. A line, once sent, stays on its message in every later turn
# (what the model saw does not change under it); the trajectory on disk never has it. Nothing is gated on it: the runner's
# fail-fast and gates are unchanged. Each nudge prints one "[draft-nudge]" line to the livelog.
# MINI_DRAFT_SHA (the sha256 of a rejected report: the diagnose retry's) makes a file whose bytes still hash to it count as not
# written ("is unchanged since it was rejected"): sqlmetar3 f28c586f4214 D2 was told to edit its rejected DIAGNOSIS.md, which had
# all six headings, re-read the repo for 59 steps instead and ran out of time with the report unchanged, and no nudge fired.
DRAFT_TAIL = "\n\n[runner] {used} and `{name}` {state}. {say}"
DRAFT_ENV = ("MINI_DRAFT_FILE", "MINI_DRAFT_NAME", "MINI_DRAFT_MARK", "MINI_DRAFT_ALT", "MINI_DRAFT_STEP", "MINI_DRAFT_EVERY",
             "MINI_DRAFT_CAP", "MINI_DRAFT_SAY", "MINI_DRAFT_HEADS", "MINI_DRAFT_SHA")


def draft_settings(env=None):
    """The DRAFT NUDGE settings from the environment, or None when MINI_DRAFT_FILE is unset. Never raises."""
    env = os.environ if env is None else env
    path = env.get("MINI_DRAFT_FILE") or ""
    if not path:
        return None

    def num(k, d):
        try:
            return max(0, int(env.get(k) or d))
        except ValueError:
            return d
    return {"file": path, "name": env.get("MINI_DRAFT_NAME") or Path(path).name, "mark": env.get("MINI_DRAFT_MARK") or "",
            "alt": [a for a in (env.get("MINI_DRAFT_ALT") or "").split(",") if a.strip()],
            "heads": [h.strip() for h in (env.get("MINI_DRAFT_HEADS") or "").split("|") if h.strip()],
            "sha": (env.get("MINI_DRAFT_SHA") or "").strip().lower(),
            "first": max(1, num("MINI_DRAFT_STEP", 10)), "every": num("MINI_DRAFT_EVERY", 0), "cap": num("MINI_DRAFT_CAP", 0),
            "say": " ".join((env.get("MINI_DRAFT_SAY") or "Write it now, then improve it.").split())}


def _has_text(path):
    try:
        return bool(Path(path).read_text(errors="replace").strip())
    except (OSError, ValueError):
        return False


def missing_heads(text, heads):
    """The names in `heads` that no `## <name>` line outside a code fence starts (diag_check.parse's rule: case-insensitive,
    after any leading numbering such as `2. `)."""
    found, fence = set(), None
    for ln in text.splitlines():
        m = re.match(r"^(`{3,}|~{3,})\s*(.*?)\s*$", ln)
        if m:
            if fence is None:
                fence = m.group(1)
            elif m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2):
                fence = None
            continue
        h = None if fence else re.match(r"^##\s+(.+?)\s*#*\s*$", ln)
        if h:
            name = h.group(1).lower().lstrip("0123456789. )")
            found.update(x for x in heads if name.startswith(x.lower()))
    return [x for x in heads if x not in found]


def draft_state(path, mark="", alt=(), heads=(), sha=""):
    """Why `path` does not count as written yet ('does not exist yet' / 'is empty' / 'still has ... placeholders' / 'has no
    `## X` heading yet' / 'is unchanged since it was rejected': its sha256 is `sha`), or None when it does, when a file in `alt`
    has text, or when it cannot be read (no nudge on a doubt). Never raises."""
    if any(_has_text(a) for a in alt):
        return None
    try:
        p = Path(path)
        if not p.exists():
            return "does not exist yet"
        raw = p.read_bytes()
    except (OSError, ValueError):
        return None
    t = raw.decode("utf-8", errors="replace")
    if not t.strip():
        return "is empty"
    if mark and mark in t:
        return f"still has the skeleton's `{mark}` placeholders"
    miss = missing_heads(t, heads) if heads else []
    if miss:
        return "has no " + ", ".join(f"`## {h}`" for h in miss) + (" heading" if len(miss) == 1 else " headings") + " yet"
    if sha and hashlib.sha256(raw).hexdigest() == sha:
        return "is unchanged since it was rejected"
    return None


def _append_text(m, s):
    """Append s to message m's text (a new list for list content: the original list is never changed)."""
    c = m.get("content")
    m["content"] = [*c, {"type": "text", "text": s}] if isinstance(c, list) else (c if isinstance(c, str) else "") + s


# PROVIDER OUTAGE (2026-10-23, job 7531cd855697): Darkbloom answered every call with "Provider capacity is temporarily
# unavailable" (litellm BadRequestError), the run made zero steps and the runner graded the untouched skeleton as a failure.
# An outage is not a model failure: query() waits it out (backoff, default cap MINI_OUTAGE_MAX_S = 7 days, 0 = none; every wait prints a heartbeat line so the livelog stays alive), the wall-time clock does not
# count the wait (see _patch_agent), and past the bound ProviderUnavailable ends the run with exit_status "ProviderUnavailable"
# (the runner turns that into the queue's PAUSED exit). A genuine error (context window, auth, a real 400) never matches.
OUTAGE_RE = re.compile(r"capacity is temporarily unavailable|temporarily unavailable|service unavailable|no (?:available )?(?:provider|worker|capacity)|"
                       r"connection (?:error|refused|reset|aborted)|connect call failed|server disconnected|remote(?:ly)? closed|"
                       r"bad gateway|gateway time-?out|overloaded|"
                       # 2026-11-02: Darkbloom lends the machine to sandbox jobs (io.darkbloom.sandbox): it unloads the
                       # model for the hold and answers model_unavailable (modelNotLoaded) until the release reloads it
                       r"model[_ ]unavailable|modelnotloaded|model (?:is )?not loaded", re.I)
OUTAGE_TYPES = ("ServiceUnavailableError", "APIConnectionError", "Timeout", "InternalServerError", "BadGatewayError", "ConnectError", "ReadTimeout")


class ProviderUnavailable(Exception):
    """The model provider stayed unavailable past MINI_OUTAGE_MAX_S."""


# MODEL NOT SERVED (review 2026-11-03): Darkbloom answers a model id it does not serve with the same "capacity is temporarily
# unavailable" text as a real outage, so a model swapped out mid-run (provider config changed, another model picked) used to
# keep the run waiting for MINI_OUTAGE_MAX_S (7 days). Each outage wait now also asks the endpoint's /models: once it has
# answered with a list that lacks this run's model for MINI_MODEL_ABSENT_MAX_S (default 1800, 0 = never give up this way),
# query() raises ModelNotServed and the runner fails the run as infrastructure (FAIL model-not-served). A sandbox hold unloads
# the model but keeps it listed, so it never trips this; an unreachable endpoint or an empty list is "unknown" and resets the count.
class ModelNotServed(Exception):
    """The endpoint kept listing models without this run's model for MINI_MODEL_ABSENT_MAX_S."""


def _models_doc(timeout=15):
    """The endpoint's (env OPENAI_API_BASE, set by the runner) /models answer as a dict, an error answer's JSON body
    included (the relay's 503 for an endpoint that does not answer still carries cpu_lane; it has no "data", so
    served_ids() reads it as unknown); None when there is no base URL or no JSON object came back. The key comes from
    litellm.api_key (the launcher's), never from env, and is never printed. Never raises."""
    base = os.environ.get("OPENAI_API_BASE", "").rstrip("/")
    if not base:
        return None
    try:
        import urllib.request
        key = getattr(sys.modules.get("litellm"), "api_key", None) or ""
        req = urllib.request.Request(base + "/models", headers={"Authorization": "Bearer " + str(key)})
        import urllib.error
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                doc = json.loads(r.read(1 << 20))
        except urllib.error.HTTPError as e:
            with e:
                doc = json.loads(e.read(1 << 20))
        return doc if isinstance(doc, dict) else None
    except Exception:
        return None


def served_ids(timeout=15):
    """Model ids the endpoint lists on /models; None when unknown (no base URL, unreachable, an odd or empty answer).
    Never raises."""
    try:
        ids = [str(m["id"]) for m in _models_doc(timeout)["data"]]
        return ids or None
    except Exception:
        return None


# RELAY WAIT (2026-11-05, isort job 3d9444be3222): on the remote lane the model is reached through the queue's relay
# (CPU_RUNNER_LLM_URL is set), which waits a Darkbloom Sandbox hold or a provider reload out INSIDE the call (up to 19 min)
# and then answers. That call succeeds, so the outage loop below never saw the wait and the agent's wall clock charged it to
# the model (D1 hit TimeExceeded after one 680 s call). The relay adds each answered call's wait to the job's total and
# reports it on /models as cpu_lane.wait_s. After a call of RELAY_READ_MIN_S or more, query() reads it, credits the growth
# (at most the call's own length) like an outage and prints a [provider-hold] line, which the runner's kill timer reads.
# PAUSE (2026-11-05): crediting after the answer comes too late when the hold outlasts the time the run has left: the
# runner's kill timer and the remote lane's wall fired while the call still waited (canvas A2, isort, param). The relay now
# also reports cpu_lane.wait_live_s, which grows while a call waits and keeps the waits of calls that then failed. While a
# call is in flight on the relay, a poller reads it every RELAY_POLL_S and credits the growth at once (at most the part of
# this attempt not credited yet), one [provider-hold] line per credit, so every clock of the run stops during the hold.
# The outage loop then takes off only what the poller did not credit, and says so (its line ends "; credited Ns" too), so
# the runner adds up the credit lines and nothing counts twice. A drop in the count (the queue API restarted and counts
# from 0 again) is a new baseline, never credit.
RELAY_READ_MIN_S = 3.0   # the relay's shortest wait is one retry delay (3 s), so a shorter call cannot have waited
RELAY_POLL_S = 5.0       # a call in flight on the relay asks it this often; a clock with less than this left can still fire
_SAY = threading.Lock()


def _say(line):
    """One whole line to stderr in a single write (the relay poller prints from its own thread). Never raises."""
    with _SAY:
        try:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
        except Exception:
            pass


def relay_wait_s(timeout=15):
    """The relay's wait count for this job: cpu_lane.wait_live_s (seconds its model calls waited for the provider, the
    waits still under way and those of failed calls included; it starts over from 0 when the queue API restarts), or on an
    older relay cpu_lane.wait_s (answered calls only). None when not on the relay or unknown (no answer, junk). Never
    raises."""
    if not os.environ.get("CPU_RUNNER_LLM_URL"):
        return None
    try:
        d = _models_doc(timeout)["cpu_lane"]
        v = d["wait_live_s"] if "wait_live_s" in d else d["wait_s"]
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None
    except Exception:
        return None


def is_provider_unavailable(exc):
    return type(exc).__name__ in OUTAGE_TYPES or bool(OUTAGE_RE.search(str(exc)))


def _bloom_phase():
    """bloom_control's phase (switching / ready / ...) for the heartbeat line; '?' when unreadable. Never raises."""
    try:
        import subprocess
        out = subprocess.run([sys.executable, str(DIR.parent / "bloom_control.py"), "state"], capture_output=True, text=True, timeout=10).stdout
        return (json.loads(out).get("control") or {}).get("phase") or "?"
    except Exception:
        return "?"


def _patch_agent():
    """Shift the agent's wall-clock start by the outage time its model waited, so agent.wall_time_limit_seconds measures the time the provider was serving."""
    try:
        from minisweagent.agents.default import DefaultAgent
    except Exception:
        return
    if getattr(DefaultAgent.query, "_outage_aware", False):
        return
    orig = DefaultAgent.query

    def query(self):
        try:
            return orig(self)
        finally:
            take = getattr(self.model, "take_outage", None)
            if take:
                self._start_time += take()
    query._outage_aware = True
    DefaultAgent.query = query


_patch_agent()


class CtxLitellmModel(LitellmModel):
    def __init__(self, *, config_class=CtxConfig, **kwargs):
        super().__init__(config_class=config_class, **kwargs)
        self._pending_outage = 0.0
        self._relay_seen = None   # the relay's wait count as last read (None: not read yet, or unknown)
        self._credit_lock = threading.Lock()
        self._draft = draft_settings()   # DRAFT NUDGE (None: off)
        self._nudges = {}   # message index -> the DRAFT NUDGE line appended to that message (re-sent on every later turn)
        self._next_nudge = self._draft["first"] if self._draft else 0

    def take_outage(self):
        """Seconds spent waiting for the provider since the last call (consumed by the patched DefaultAgent.query)."""
        with self._credit_lock:
            t, self._pending_outage = self._pending_outage, 0.0
        return t

    def _add_outage(self, secs):
        with self._credit_lock:
            self._pending_outage += secs

    def query(self, messages, **kwargs):
        limit = float(os.environ.get("MINI_OUTAGE_MAX_S", "604800")) or 604800.0 * 52  # 7 days; 0 = (effectively) no cap: wait until Darkbloom serves again
        base = float(os.environ.get("MINI_OUTAGE_BACKOFF_S", "15"))
        absent_max = float(os.environ.get("MINI_MODEL_ABSENT_MAX_S", "1800"))
        name = str(self.config.model_name)
        want = name[len("openai/"):] if name.startswith("openai/") else name  # the runner passes -m openai/<served id>
        episode, n, absent_since = 0.0, 0, None
        relay = bool(os.environ.get("CPU_RUNNER_LLM_URL"))
        while True:
            if relay and self._relay_seen is None:
                v = relay_wait_s()
                with self._credit_lock:
                    self._relay_seen = v   # the baseline, never a credit
            att = {"t0": time.time(), "credited": 0.0, "closed": False}
            stop = threading.Event()
            if relay:
                threading.Thread(target=self._relay_poll, args=(att, stop), daemon=True, name="relay-wait").start()
            try:
                try:
                    out = super().query(messages, **kwargs)
                finally:
                    stop.set()
            except Exception as e:
                if not is_provider_unavailable(e):
                    with self._credit_lock:
                        att["closed"] = True
                    raise
                spent = time.time() - att["t0"]
                if relay:   # what the relay waited in this attempt is part of `spent`: its count is the new baseline
                    v = relay_wait_s()
                    with self._credit_lock:
                        att["closed"], self._relay_seen = True, v
                rest = max(0.0, spent - att["credited"])   # the poller credited the rest already
                note = ""
                if absent_max > 0:
                    ids = served_ids()
                    if ids and want not in ids:
                        absent_since = absent_since if absent_since is not None else time.time()
                        gone = time.time() - absent_since
                        if gone >= absent_max:
                            self._add_outage(rest)
                            raise ModelNotServed(f"model {want!r} not listed on the endpoint's /models for {int(gone)}s (lists {ids[:6]}): {str(e)[:200]}") from e
                        note = f", model {want!r} not listed on /models for {gone:.0f}s of {absent_max:.0f}s"
                    else:
                        absent_since = None
                if episode + spent >= limit:
                    self._add_outage(rest)
                    raise ProviderUnavailable(f"provider unavailable for {int(episode + spent)}s: {str(e)[:200]}") from e
                nap = min(120.0, base * (2 ** n), limit - episode - spent)
                n += 1
                credit = rest + nap
                self._add_outage(credit)
                _say(f"[provider-outage] unavailable ({type(e).__name__}); waiting {nap:.0f}s, outage so far {episode + spent + nap:.0f}s of {limit:.0f}s "
                     f"(waiting for Darkbloom, phase={_bloom_phase()}, {(episode + spent + nap) / 60:.0f}m{note}); credited {credit:.1f}s")
                time.sleep(nap)
                episode += spent + nap
            else:
                self._relay_credit(att)
                return out

    def _relay_poll(self, att, stop):
        """While the attempt is in flight: credit the relay's wait as it grows (see PAUSE). Never raises."""
        while not stop.wait(RELAY_POLL_S):
            v = relay_wait_s()
            if v is not None and not stop.is_set():
                self._relay_apply(att, v, poll=True)

    def _relay_apply(self, att, v, poll):
        """Credit the growth of the relay's wait count `v` since the last read, at most the part of this attempt not
        credited yet (growth past it is dropped); the first read, or a drop (the queue API restarted), is the new baseline.
        A closed attempt takes no poller credit. Returns the credit; a line for any credit of 0.05 s or more. Never raises."""
        with self._credit_lock:
            if poll and att["closed"]:
                return 0.0
            seen, self._relay_seen = self._relay_seen, v
            if seen is None or v <= seen:
                return 0.0
            credit = min(v - seen, max(0.0, time.time() - att["t0"] - att["credited"]))
            if credit <= 0:
                return 0.0
            att["credited"] += credit
            self._pending_outage += credit
            waited, took = att["credited"], time.time() - att["t0"]
        if credit < 0.05:
            return credit
        if poll:
            _say(f"[provider-hold] the call has waited {waited:.0f}s so far in the relay for the provider (a Darkbloom Sandbox "
                 f"hold or a model reload); credited {credit:.1f}s")
        else:
            _say(f"[provider-hold] the call took {took:.0f}s, {waited:.0f}s of it in the relay waiting for the provider (a Darkbloom "
                 f"Sandbox hold or a model reload); credited {credit:.1f}s")
        return credit

    def _relay_credit(self, att):
        """After an answered call: close the attempt to the poller, then credit what the relay's count grew by that the poller
        did not credit (see RELAY WAIT). A call under RELAY_READ_MIN_S cannot have waited: no read. Never raises."""
        with self._credit_lock:
            att["closed"] = True
        if not os.environ.get("CPU_RUNNER_LLM_URL") or time.time() - att["t0"] < RELAY_READ_MIN_S:
            return
        v = relay_wait_s()
        if v is not None:
            self._relay_apply(att, v, poll=False)

    def _draft_nudge(self, msgs):
        """See DRAFT NUDGE. msgs is shrink_messages' copy (fresh dicts: the trajectory's own are never touched). A call repeated
        for the same turn (mini's retry, the outage loop) adds nothing new. Never raises."""
        d = self._draft
        if not d:
            return msgs
        try:
            last = len(msgs) - 1
            k = sum(1 for m in msgs if m.get("role") == "assistant")   # steps taken so far
            if k >= self._next_nudge and last >= 2 and last not in self._nudges and msgs[last].get("role") in ("tool", "user"):
                state = draft_state(d["file"], d["mark"], d["alt"], d.get("heads") or (), d.get("sha") or "")
                if state:
                    used = f"You have used {k} of your {d['cap']} steps" if d["cap"] else f"You have used {k} steps"
                    self._nudges[last] = DRAFT_TAIL.format(used=used, name=d["name"], state=state, say=d["say"])
                    self._next_nudge = k + d["every"] if d["every"] else float("inf")
                    _say(f"[draft-nudge] step {k}: `{d['name']}` {state}; reminded"
                         + (f" (again at step {self._next_nudge} if still not written)" if d["every"] else " (once)"))
            for i, s in self._nudges.items():
                if i < len(msgs):
                    _append_text(msgs[i], s)
        except Exception as e:
            _say(f"[draft-nudge] error, no nudge: {type(e).__name__}: {e}")
        return msgs

    def _prepare_messages_for_api(self, messages):
        return super()._prepare_messages_for_api(self._draft_nudge(shrink_messages(messages, self.config)))


def replay(traj, cfg=None):
    """Offline: for each assistant turn of a saved trajectory, rewrite the history that turn would have seen.
    Returns [(step, raw_tokens, sent_tokens)]."""
    import json
    cfg = cfg or CtxConfig(model_name="x")
    msgs = [{k: v for k, v in m.items() if k != "extra"} for m in json.loads(Path(traj).read_text())["messages"]]
    out = []
    for i, m in enumerate(msgs):
        if m.get("role") == "assistant":
            hist = msgs[:i]
            sent = shrink_messages(hist, cfg)
            lt = latest_test_run(hist)
            if lt is not None and estimate_tokens(sent) < cfg.ceiling_tokens * 0.9:  # (under the ceiling the latest test run must be byte-identical)
                assert _text(sent[lt]) == _text(hist[lt]), f"latest test output changed at step {len(out) + 1}"
            out.append((len(out) + 1, estimate_tokens(hist), estimate_tokens(sent)))
    return out


def _toy(n, big=2000):
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task " * 50}]
    for i in range(n):
        msgs.append({"role": "assistant", "content": f"thought {i} " * 50 + "\n</think>\n\nrun it", "tool_calls": [
            {"id": str(i), "type": "function", "function": {"name": "bash", "arguments": '{"command": "ls"}'}}]})
        out = ("TAP version 13\nnot ok 1 - boom\nAssertionError LASTFAIL\n" if i == n - 4 else f"line {i}\n") + "x" * big
        msgs.append({"role": "tool", "tool_call_id": str(i), "content": out})
    return msgs


def _self_test_model_absent():
    """served_ids against a fake local /models server (key sent as a bearer, never printed; error, empty or no base = None),
    then the outage loop with a scripted served_ids: a list without the model gives up as ModelNotServed after
    MINI_MODEL_ABSENT_MAX_S; a list with it, an unknown answer, or absences broken by listings keep waiting (ProviderUnavailable
    at the outage bound); 0 turns the check off."""
    import contextlib
    import http.server
    import io
    import threading
    seen, answer = [], {"code": 200, "body": {"data": [{"id": "other"}, {"id": "x"}]}}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            body = json.dumps(answer["body"]).encode()
            self.send_response(answer["code"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    lit = sys.modules.get("litellm")
    saved_key = getattr(lit, "api_key", None)
    try:
        lit.api_key = "k-SECRET-SELFTEST"
        assert served_ids() is None, "no OPENAI_API_BASE: unknown"
        os.environ["OPENAI_API_BASE"] = f"http://127.0.0.1:{srv.server_port}/v1/"
        err = io.StringIO()
        with contextlib.redirect_stdout(err), contextlib.redirect_stderr(err):
            assert served_ids() == ["other", "x"], seen
            answer["body"] = {"data": []}
            assert served_ids() is None, "an empty list is unknown"
            answer.update(code=500, body={"error": "boom"})
            assert served_ids() is None, "an error is unknown"
        assert seen and all(pth == "/v1/models" and auth == "Bearer k-SECRET-SELFTEST" for pth, auth in seen), seen
        assert "k-SECRET" not in err.getvalue(), "key printed"
    finally:
        lit.api_key = saved_key
        os.environ.pop("OPENAI_API_BASE", None)
        srv.shutdown()
        srv.server_close()

    g, real_q = globals(), LitellmModel.query
    real_served, real_phase = g["served_ids"], g["_bloom_phase"]
    down = Exception("OpenAIException - Provider capacity is temporarily unavailable.")

    def outcome(script, env):
        """Run one query() against a provider that is always down while served_ids answers from `script` (cycled)."""
        k = [0]

        def fake():
            k[0] += 1
            return script[(k[0] - 1) % len(script)]
        g["served_ids"], g["_bloom_phase"] = fake, (lambda: "selftest")
        os.environ.update(MINI_OUTAGE_BACKOFF_S="0.01", **env)
        LitellmModel.query = lambda self, m, **kw: (_ for _ in ()).throw(down)
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                CtxLitellmModel(model_name="openai/x", cost_tracking="ignore_errors").query([])
        except (ModelNotServed, ProviderUnavailable) as e:
            return type(e).__name__, str(e), err.getvalue(), k[0]
        finally:
            LitellmModel.query = real_q
            g["served_ids"], g["_bloom_phase"] = real_served, real_phase
            for key in ("MINI_OUTAGE_BACKOFF_S", "MINI_OUTAGE_MAX_S", "MINI_MODEL_ABSENT_MAX_S"):
                os.environ.pop(key, None)
        raise AssertionError("query() returned while the provider was down")
    absent, listed = ["other", "y"], ["other", "x"]
    name, msg, log, _ = outcome([absent], {"MINI_OUTAGE_MAX_S": "30", "MINI_MODEL_ABSENT_MAX_S": "0.05"})
    assert name == "ModelNotServed" and "'x' not listed" in msg and "capacity" in msg, (name, msg)
    assert re.search(r"outage so far \d+s of \d+s .*model 'x' not listed on /models for", log), log
    for script in ([listed], [None], [absent, listed]):  # served, unknown, or absences broken by listings: wait to the outage bound
        name, msg, _, calls = outcome(script, {"MINI_OUTAGE_MAX_S": "0.4", "MINI_MODEL_ABSENT_MAX_S": "0.05"})
        assert name == "ProviderUnavailable" and calls >= 4, (script, name, msg, calls)
    name, _, _, calls = outcome([absent], {"MINI_OUTAGE_MAX_S": "0.3", "MINI_MODEL_ABSENT_MAX_S": "0"})
    assert name == "ProviderUnavailable" and calls == 0, "MINI_MODEL_ABSENT_MAX_S=0 must not even ask /models"


def _self_test_relay_wait():
    """relay_wait_s against a fake relay /models (asked only with CPU_RUNNER_LLM_URL set; wait_live_s first, else an older
    relay's wait_s; junk = None), then query() on the relay: the first read is the baseline; while the call is in flight a
    poller credits the count's growth, at most the attempt's own length, and the answer the rest; a drop is a new baseline;
    the outage loop takes off only what the poller did not credit; no poller off the relay or after the call; every credit
    is one line in the form the runner's CREDIT_LINE reads, and the lines add up to take_outage()."""
    import contextlib
    import http.server
    import io
    st = {"doc": {"object": "list", "data": [{"id": "x"}], "cpu_lane": {"wait_s": 10.0}}, "gets": 0}
    lk = threading.Lock()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            with lk:
                st["gets"] += 1
                body, code = json.dumps(st["doc"]).encode(), st.get("code", 200)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    g = globals()
    real = (LitellmModel.query, g["RELAY_READ_MIN_S"], g["RELAY_POLL_S"], g["_bloom_phase"])
    line_re = re.compile(r"^\[provider-(?:outage|hold)\].*; credited (\d+(?:\.\d+)?)s$")  # mini-swe-runner CREDIT_LINE

    def grow(by, secs, steps=3):  # the relay waits for the provider inside the call: its count grows `by` over `secs`
        for _ in range(steps):
            time.sleep(secs / steps)
            with lk:
                st["doc"]["cpu_lane"]["wait_live_s"] += by / steps

    def count(v=None):
        with lk:
            if v is not None:
                st["doc"]["cpu_lane"]["wait_live_s"] = v
            return st["doc"]["cpu_lane"]["wait_live_s"]

    def run(mod, fn):
        """One query() with LitellmModel.query = fn -> (credits read off the lines, the lines, take_outage())."""
        LitellmModel.query = fn
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert mod.query([]) == {"ok": 1}
        lines = [ln for ln in err.getvalue().splitlines() if ln.startswith("[provider-")]
        assert all(line_re.match(ln) for ln in lines), lines
        got, total = [float(line_re.match(ln).group(1)) for ln in lines], mod.take_outage()
        assert abs(total - sum(got)) <= 0.05 * len(got) + 0.05, (total, got, lines)  # the lines round to 0.1 s
        return got, lines, total

    def answered(self, messages, **kw):
        return {"ok": 1}
    try:
        os.environ["OPENAI_API_BASE"] = "http://127.0.0.1:%d/v1" % srv.server_address[1]
        assert relay_wait_s() is None and st["gets"] == 0, "not on the relay: never asks"
        mod = CtxLitellmModel(model_name="openai/x", cost_tracking="ignore_errors")
        g["RELAY_READ_MIN_S"], g["RELAY_POLL_S"], g["_bloom_phase"] = 0.0, 0.05, (lambda: "selftest")
        LitellmModel.query = lambda self, m, **kw: (time.sleep(0.2), {"ok": 1})[1]
        assert mod.query([]) == {"ok": 1} and st["gets"] == 0, "off the relay: no poller, no read"
        os.environ["CPU_RUNNER_LLM_URL"] = os.environ["OPENAI_API_BASE"]
        assert relay_wait_s() == 10.0, "an older relay: wait_s"
        st["doc"]["cpu_lane"] = {"wait_s": 10.0, "wait_live_s": 12.5}
        assert relay_wait_s() == 12.5, "wait_live_s first"
        for junk in ("junk", True, -1, None):
            st["doc"]["cpu_lane"] = {"wait_s": 10.0, "wait_live_s": junk}
            assert relay_wait_s() is None, junk
        ok_doc = st["doc"]
        st["code"], st["doc"] = 503, {"error": {"message": "the local model endpoint is not answering", "type": "provider_unavailable"},
                                      "cpu_lane": {"wait_s": 0.0, "wait_live_s": 7.5}}
        assert relay_wait_s() == 7.5 and served_ids() is None, "the relay's 503 (endpoint not answering) still carries the count"
        st["code"], st["doc"] = 200, ok_doc
        del st["doc"]["cpu_lane"]
        assert relay_wait_s() is None and served_ids() == ["x"], "no cpu_lane: unknown, and /models still reads"
        st["doc"]["cpu_lane"] = {"wait_s": 0.0, "wait_live_s": 100.0}

        mod = CtxLitellmModel(model_name="openai/x", cost_tracking="ignore_errors")
        got, lines, total = run(mod, lambda self, m, **kw: (grow(0.6, 0.75), time.sleep(0.05), {"ok": 1})[2])
        assert any("so far in the relay" in ln for ln in lines), ("credited while the call was in flight", lines)
        assert 0.5 <= total <= 0.6 + 1e-9 and abs(mod._relay_seen - 100.6) < 1e-6, (total, mod._relay_seen)
        n = st["gets"]
        time.sleep(0.2)
        count(count() + 5.0)
        time.sleep(0.2)
        assert st["gets"] == n and mod.take_outage() == 0, "the poller stops with the call"

        count(3.0)   # the queue API restarted: it counts from 0 again
        got, lines, total = run(mod, lambda self, m, **kw: (time.sleep(0.2), grow(0.3, 0.3), time.sleep(0.05), {"ok": 1})[3])
        assert 0.25 <= total <= 0.3 + 1e-9 and abs(mod._relay_seen - 3.3) < 1e-6, ("a drop is a new baseline", total, mod._relay_seen)

        def jump(self, m, **kw):  # the count jumps far past the attempt's own length (a call the run gave up on still waiting)
            count(count() + 30.0)
            time.sleep(0.3)
            return {"ok": 1}
        got, lines, total = run(mod, jump)
        assert total <= 0.3 + 1e-9 and mod._relay_seen >= 33.3 - 1e-6, ("at most the attempt's length", total, mod._relay_seen)

        k = [0]

        def flaky(self, m, **kw):  # the relay waited inside the call, then the call failed; the retry is answered
            k[0] += 1
            if k[0] == 1:
                grow(0.3, 0.3)
                time.sleep(0.1)
                raise Exception("OpenAIException - Provider capacity is temporarily unavailable.")
            return {"ok": 1}
        os.environ["MINI_OUTAGE_BACKOFF_S"] = "0.2"
        try:
            t0 = time.time()
            got, lines, total = run(mod, flaky)
            took = time.time() - t0
        finally:
            os.environ.pop("MINI_OUTAGE_BACKOFF_S", None)
        out = [ln for ln in lines if ln.startswith("[provider-outage]")]
        assert len(out) == 1 and k[0] == 2 and 0.5 <= total <= took + 0.05, (lines, k, total, took)
        assert any("so far in the relay" in ln for ln in lines), lines

        fresh = CtxLitellmModel(model_name="openai/x", cost_tracking="ignore_errors")
        count(130.0)
        got, lines, total = run(fresh, answered)
        assert total == 0 and not lines and fresh._relay_seen == 130.0, "the first read is the baseline, never a credit"
        g["RELAY_READ_MIN_S"] = real[1]
        n = st["gets"]
        count(140.0)
        got, lines, total = run(fresh, answered)
        assert total == 0 and st["gets"] == n, "a call under RELAY_READ_MIN_S: no read, no credit"
    finally:
        LitellmModel.query, g["RELAY_READ_MIN_S"], g["RELAY_POLL_S"], g["_bloom_phase"] = real
        for k in ("OPENAI_API_BASE", "CPU_RUNNER_LLM_URL"):
            os.environ.pop(k, None)
        srv.shutdown()
        srv.server_close()


def _self_test_draft_nudge():
    """DRAFT NUDGE: off without MINI_DRAFT_FILE; due at MINI_DRAFT_STEP, then every MINI_DRAFT_EVERY steps (0: once) while the file
    is missing, empty or still holds the mark; appended to the API copy of the latest observation only (tool or user role, never
    the first two messages), the trajectory's dicts untouched; sticky on later turns; nothing new on a repeated call for the same
    turn; silent once the file (or an alt file) has text; list content gets a text part; one livelog line per nudge."""
    import contextlib
    import io
    import tempfile
    saved = {k: os.environ.pop(k, None) for k in DRAFT_ENV}
    d = Path(tempfile.mkdtemp())
    f, alt = d / "DIAGNOSIS.md", d / "NEEDS.md"
    line_re = re.compile(r"^\[draft-nudge\] step (\d+): `DIAGNOSIS\.md` (.+); reminded")

    def turn(mod, msgs):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            api = mod._prepare_messages_for_api(msgs)
        out = err.getvalue().splitlines()
        assert all(line_re.match(ln) for ln in out), out
        return api, [int(line_re.match(ln).group(1)) for ln in out], out

    def walk(mod, hist, upto, each=None):
        """Turns k = 0..upto (k assistant steps taken, the last message an observation); the steps that nudged."""
        got = []
        for k in range(upto + 1):
            api, ks, _ = turn(mod, hist[:2 + 2 * k])
            got += ks
            if each:
                each(k, api)
        return got

    def model():
        return CtxLitellmModel(model_name="openai/x", cost_tracking="ignore_errors")
    hist = _toy(30, big=50)
    orig = [dict(m) for m in hist]
    try:
        assert draft_settings() is None and walk(model(), hist, 20) == [], "off without MINI_DRAFT_FILE"
        os.environ.update(MINI_DRAFT_FILE=str(f), MINI_DRAFT_STEP="5", MINI_DRAFT_EVERY="3", MINI_DRAFT_CAP="30",
                          MINI_DRAFT_SAY="Write it\n   now.")
        mod, sent = model(), {}

        def each(k, api):
            tails = {i: _text(m) for i, m in enumerate(api) if "[runner]" in _text(m)}
            assert all(_text(api[i]).startswith(_text(hist[i])) for i in tails), "a nudge is appended, never replaces"
            for i, t in sent.items():
                assert tails.get(i) == t, (k, i, "a sent nudge stays as it was on every later turn")
            sent.update(tails)
        assert walk(mod, hist, 15, each) == [5, 8, 11, 14], "missing: due at 5, then every 3"
        assert hist == orig, "the trajectory's messages were changed"
        assert sorted(sent) == [11, 17, 23, 29], sorted(sent)   # the latest observation of steps 5, 8, 11, 14
        assert sent[11].endswith("\n\n[runner] You have used 5 of your 30 steps and `DIAGNOSIS.md` does not exist yet. Write it now."), sent[11]
        api, ks, _ = turn(mod, hist[:2 + 2 * 17])
        api2, ks2, _ = turn(mod, hist[:2 + 2 * 17])
        assert ks == [17] and ks2 == [] and api == api2, "a repeated call for the same turn adds nothing new"
        assert sum("[runner]" in _text(m) for m in api) == 5
        early = model()
        os.environ["MINI_DRAFT_EVERY"] = "0"
        assert walk(model(), hist, 20) == [5], "MINI_DRAFT_EVERY=0: once"
        os.environ["MINI_DRAFT_EVERY"] = "3"
        assert walk(early, hist, 20) == [5, 8, 11, 14, 17, 20], "settings are read when the model is made"
        f.write_text("  \n")
        _, ks, out = turn(model(), hist[:2 + 2 * 6])
        assert ks == [6] and "is empty" in out[0], out
        os.environ["MINI_DRAFT_MARK"] = "TODO(mini-skeleton)"
        f.write_text("def test_x():\n    assert False  # TODO(mini-skeleton)\n")
        api, ks, out = turn(model(), hist[:2 + 2 * 6])
        assert ks == [6] and "still has the skeleton's `TODO(mini-skeleton)` placeholders" in _text(api[-1]), out
        f.write_text("def test_x():\n    assert f() == 2\n")
        m2 = model()
        assert walk(m2, hist, 12) == [], "written: no nudge"
        f.unlink()
        _, ks, _ = turn(m2, hist[:2 + 2 * 13])
        assert ks == [13], "the file gone again: due at once"
        os.environ["MINI_DRAFT_ALT"] = f"{alt},"
        alt.write_text("## Question\nwhich?\n")
        assert walk(model(), hist, 12) == [], "an alt file with text counts as written"
        alt.write_text("")
        assert walk(model(), hist, 6) == [5], "an empty alt file does not"
        os.environ.pop("MINI_DRAFT_ALT")
        os.environ["MINI_DRAFT_HEADS"] = "Code path| Root cause |"
        f.write_text("## Code path\n```python a.py:1-3\n## Root cause\nx = 1\n```\n")
        api, ks, out = turn(model(), hist[:2 + 2 * 6])
        assert ks == [6] and "`DIAGNOSIS.md` has no `## Root cause` heading yet." in _text(api[-1]), out  # a heading in a fence is code
        f.write_text("x\n")
        _, ks, out = turn(model(), hist[:2 + 2 * 6])
        assert ks == [6] and "has no `## Code path`, `## Root cause` headings yet" in out[0], out
        f.write_text("## Code path\n```python a.py:1-3\nx = 1\n```\n## 2. Root Cause (one sentence)\nf() is wrong.\n")
        assert walk(model(), hist, 12) == [], "every heading present: written"
        assert missing_heads("~~~\n## Root cause\n~~~\n", ["Root cause"]) == ["Root cause"]
        assert missing_heads("```py x:1\n````\n## Root cause\n", ["Root cause"]) == [], "a longer bare fence closes it"
        rejected = f.read_bytes()  # MINI_DRAFT_SHA: the retry's rejected report, every heading present, does not count as written
        os.environ["MINI_DRAFT_SHA"] = " " + hashlib.sha256(rejected).hexdigest().upper() + "\n"
        api, ks, out = turn(model(), hist[:2 + 2 * 6])
        assert ks == [6] and _text(api[-1]).endswith("\n\n[runner] You have used 6 of your 30 steps and `DIAGNOSIS.md` is unchanged "
                                                     "since it was rejected. Write it now."), (out, _text(api[-1])[-200:])
        f.write_text("## Code path\n```python a.py:1-3\nx = 1\n```\n## Root cause\nf() is wrong.\n")
        assert walk(model(), hist, 12) == [], "edited since the rejection: written"
        f.write_bytes(rejected)
        _, ks, _ = turn(model(), hist[:2 + 2 * 6])
        assert ks == [6], "the rejected bytes again: not written"
        f.write_text("## Code path\n")
        _, ks, out = turn(model(), hist[:2 + 2 * 6])
        assert ks == [6] and "has no `## Root cause` heading yet" in out[0], out  # a missing heading is said first
        assert draft_state(str(f), sha="") is None and draft_state(str(d / "nope"), sha="ab") == "does not exist yet"
        os.environ.pop("MINI_DRAFT_SHA")
        os.environ.pop("MINI_DRAFT_HEADS")
        f.unlink()
        for junk in ("junk", "-3", ""):
            os.environ["MINI_DRAFT_STEP"] = junk
            assert model()._draft["first"] in (10, 1), junk
        os.environ.update(MINI_DRAFT_STEP="2", MINI_DRAFT_EVERY="0")
        os.environ.pop("MINI_DRAFT_CAP")
        lst = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
        for i in range(3):
            lst += [{"role": "assistant", "content": "go"}, {"role": "user", "content": [{"type": "text", "text": f"out {i}"}]}]
        before = json.dumps(lst)
        api, ks, _ = turn(model(), lst)
        assert ks == [3] and json.dumps(lst) == before, "user observations; list content copied, never changed"
        assert api[-1]["content"][0] == {"type": "text", "text": "out 2"} and api[-1]["content"][1]["text"].startswith(
            "\n\n[runner] You have used 3 steps and"), api[-1]
        _, ks, _ = turn(model(), lst + [{"role": "assistant", "content": "a"}])
        assert ks == [], "the latest message is not an observation: no nudge"
        os.environ["MINI_DRAFT_STEP"] = "1"
        _, ks, _ = turn(model(), [{"role": "assistant", "content": "a"}, {"role": "user", "content": "b"}])
        assert ks == [], "never the first two messages"
        bad = model()
        bad._draft["file"] = None   # a broken setting: the run goes on without a nudge
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            api = bad._prepare_messages_for_api(lst)
        assert "error, no nudge" in err.getvalue() and all("[runner]" not in _text(m) for m in api)
    finally:
        for k in DRAFT_ENV:
            os.environ.pop(k, None)
        os.environ.update({k: v for k, v in saved.items() if v is not None})
        __import__("shutil").rmtree(d, ignore_errors=True)


def self_test():
    cfg = CtxConfig(model_name="x")
    assert cfg.strip_old_thinking is False and cfg.shrink_obs and cfg.ceiling_tokens == 32000
    m = _toy(20)
    orig = [dict(x) for x in m]
    r = shrink_messages(m, cfg)
    assert m == orig, "input mutated"
    tools = [x for x in r if x["role"] == "tool"]
    assert all(len(t["content"]) > 1500 for t in tools[-6:]), "last 6 whole"
    assert "rerun the command" in tools[0]["content"] and len(tools[0]["content"]) < 450
    assert "LASTFAIL" in tools[16]["content"] and len(tools[16]["content"]) > 1500, "latest test run kept whole"
    assert all("</think>" in x["content"] for x in r if x["role"] == "assistant"), "thinking kept by default"
    s = shrink_messages(m, CtxConfig(model_name="x", strip_old_thinking=True))
    a = [x["content"] for x in s if x["role"] == "assistant"]
    assert all("</think>" not in c for c in a[:-1]) and "</think>" in a[-1]
    assert [x["tool_call_id"] for x in s if x["role"] == "tool"] == [str(i) for i in range(20)]
    big = _toy(60, big=20000)
    assert estimate_tokens(big) > 32000
    sh = shrink_messages(big, cfg)
    assert estimate_tokens(sh) <= 32000, estimate_tokens(sh)
    assert estimate_tokens(shrink_messages(big, CtxConfig(model_name="x", ceiling=False))) > 32000, "ceiling is what trims it"
    assert sum(len(x["content"]) > 3000 for x in sh if x["role"] == "tool") <= 3 + 1
    off = shrink_messages(big, CtxConfig(model_name="x", shrink_obs=False, ceiling=False))
    assert off == big
    # the model class prepares API messages without touching the originals and drops `extra`
    tr = Path(__import__("tempfile").mkdtemp()) / "t.json"
    tr.write_text(__import__("json").dumps({"messages": _toy(40, big=3000)}))
    rr = replay(tr)
    assert len(rr) == 40 and max(r[2] for r in rr) <= 32000 and max(r[1] for r in rr) > 32000
    mod = CtxLitellmModel(model_name="openai/x", cost_tracking="ignore_errors")
    api = mod._prepare_messages_for_api([dict(x, extra={"a": 1}) for x in _toy(10)])
    assert all("extra" not in x for x in api)
    # the latest REAL test run is protected, not a later grep/cat that merely shows test-looking text
    tc = lambda i, cmd: {"role": "assistant", "content": "", "tool_calls": [{"id": str(i), "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": cmd})}}]}
    fake = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
    for i in range(12):
        cmd = "cd sidecar && node --test t.js 2>&1 | tail -50" if i == 2 else ("grep -n 'not ok' log.txt" if i == 3 else f"ls {i}")
        out = ("TAP version 13\nnot ok 1 - REALRUN\n" if i == 2 else "not ok 9 - GREPPED\n" if i == 3 else "") + f"o{i}\n" + "x" * 2000
        fake += [tc(i, cmd), {"role": "tool", "tool_call_id": str(i), "content": out}]
    assert latest_test_run(fake) == 2 * 2 + 3  # index of the tool message of step 2
    r2 = [x for x in shrink_messages(fake, cfg) if x["role"] == "tool"]
    assert "REALRUN" in r2[2]["content"] and len(r2[2]["content"]) > 1500 and "GREPPED" in r2[3]["content"] and len(r2[3]["content"]) < 450, "real run kept, grep shrunk"
    # assistant stubs never leave a dangling <think>
    assert _stub("<think>" + "a" * 500 + "</think>\nanswer " * 50).startswith("answer")
    assert _stub("<think>" + "a" * 500).endswith("</think>") and _stub("plain " * 100) == ("plain " * 100)[:200]
    # stable_prefix: shrunk set grows in blocks; between block steps each turn's prompt extends the previous one byte for byte
    def common(a, b):
        n = 0
        while n < min(len(a), len(b)) and a[n] == b[n]:
            n += 1
        return n
    full = _toy(40, big=2000)
    stab, brk_s, brk_d, off_ok = CtxConfig(model_name="x", stable_prefix=True, ceiling=False), 0, 0, True
    prev_s = prev_d = None
    for k in range(1, 41):
        h = full[:2 + 2 * k - 1]  # history a turn sees: ends with the tool output of step k-1 (k>=1)
        cur_s, cur_d = shrink_messages(h, stab), shrink_messages(h, CtxConfig(model_name="x", ceiling=False))
        if prev_s is not None:
            brk_s += common(prev_s, cur_s) < len(prev_s)
            brk_d += common(prev_d, cur_d) < len(prev_d)
        prev_s, prev_d = cur_s, cur_d
    assert brk_s <= 6 and brk_d >= 25 and brk_s * 3 < brk_d, (brk_s, brk_d)  # default breaks the prefix nearly every turn; stable only at block steps / the test-run hand-over
    h = shrink_messages(full, stab)
    st = [x for x in h if x["role"] == "tool"]
    assert len(st) == 40 and sum("rerun the command" in x["content"] for x in st) == 30 and all("rerun the command" not in x["content"] for x in st[30:]), "block edge (40-6)//6*6"
    assert "LASTFAIL" in st[36]["content"] and len(st[36]["content"]) > 1500 and all(len(x["content"]) > 1500 for x in st[-6:])
    assert shrink_messages(full, CtxConfig(model_name="x", stable_prefix=True, shrink_obs=False, ceiling=False)) == full
    assert CtxConfig(model_name="x").stable_prefix is False
    # provider outage: classified narrowly, waited out with the wait reported to the agent's wall clock, bounded, genuine errors untouched
    assert is_provider_unavailable(Exception("litellm.BadRequestError: OpenAIException - Provider capacity is temporarily unavailable."))
    assert is_provider_unavailable(Exception("Connection error.")) and not is_provider_unavailable(Exception("This model's maximum context length is 32768 tokens"))
    assert not is_provider_unavailable(ValueError("400 bad request: invalid tool schema"))
    assert is_provider_unavailable(Exception("OpenAIException - model_unavailable (modelNotLoaded, model_load)"))
    assert is_provider_unavailable(Exception("Error code: 503 - model not loaded")) and not is_provider_unavailable(Exception("model 'x' not found"))
    assert "604800" in Path(__file__).read_text() and "waiting for Darkbloom, phase=" in Path(__file__).read_text()
    saved_env = {k: os.environ.pop(k, None) for k in ("OPENAI_API_BASE", "MINI_MODEL_ABSENT_MAX_S", "CPU_RUNNER_LLM_URL")}  # never ask a real endpoint
    os.environ.update(MINI_OUTAGE_BACKOFF_S="0.01", MINI_OUTAGE_MAX_S="0.5")
    real, calls = LitellmModel.query, []

    def flaky(self, messages, **kw):
        calls.append(1)
        if len(calls) <= 3:
            raise Exception("OpenAIException - Provider capacity is temporarily unavailable.")
        return {"ok": 1}
    try:
        LitellmModel.query = flaky
        mod2 = CtxLitellmModel(model_name="openai/x", cost_tracking="ignore_errors")
        assert mod2.query([]) == {"ok": 1} and len(calls) == 4 and mod2.take_outage() > 0 and mod2.take_outage() == 0
        LitellmModel.query = lambda self, m, **k: (_ for _ in ()).throw(Exception("OpenAIException - Provider capacity is temporarily unavailable."))
        try:
            mod2.query([])
            raise AssertionError("expected ProviderUnavailable")
        except ProviderUnavailable:
            pass
        assert mod2.take_outage() > 0
        LitellmModel.query = lambda self, m, **k: (_ for _ in ()).throw(ValueError("This model's maximum context length is 32768 tokens"))
        try:
            mod2.query([])
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
    finally:
        LitellmModel.query = real
        for k in ("MINI_OUTAGE_BACKOFF_S", "MINI_OUTAGE_MAX_S"):
            os.environ.pop(k, None)
    try:
        _self_test_model_absent()
        _self_test_relay_wait()
        _self_test_draft_nudge()
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    from minisweagent.agents.default import DefaultAgent
    saved = DefaultAgent.query
    try:
        DefaultAgent.query = lambda self: "m"
        _patch_agent()
        ag = DefaultAgent.__new__(DefaultAgent)
        ag._start_time, ag.model = 100.0, types.SimpleNamespace(take_outage=lambda: 7.0)
        assert ag.query() == "m" and ag._start_time == 107.0, "wall clock must not count the outage"
    finally:
        DefaultAgent.query = saved
    print("mini_ctx self-test ok")


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["--replay"]:
        rows = replay(sys.argv[2])
        print(f"steps={len(rows)} max_raw={max(r[1] for r in rows)} max_sent={max(r[2] for r in rows)} (est. tokens)")
        sys.exit(0 if max(r[2] for r in rows) <= 32000 else 1)
    self_test()
