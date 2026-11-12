#!/usr/bin/env python3
"""worker_robust -- model-output robustness for ollama-worker.py (Phase 3, 2026-10-08).

Pure, side-effect-free helpers (no network, no worker globals) so each one is unit-testable
and provable red-on-revert from pipeline-canary.py. The worker imports this module and calls
the functions at the seams marked "worker_robust" in ollama-worker.py.

  strip_think(text)                     think-tag leakage: drop closed <think>..</think> blocks
  repair_tool_calls(content, names)     layered extraction for what the worker's own parser missed:
                                        Qwen3-Coder XML (tolerating unterminated/truncated blocks)
                                        -> Hermes JSON in <tool_call> (tolerating truncation)
                                        -> inline JSON {name, arguments}
                                        -> (optional, profile flag) small-model extractor callback
  diagnose_format_error(content, ...)   WHY a turn that looks like a tool call attempt is unusable
  FormatRequery                         <=3 consecutive free re-prompts, then repeated_format_error
  LoopDetector                          repeated reads / repeated writes / A->B->A / no-progress;
                                        first detection -> warn message, second -> loop_detected
  check_stop_criteria(...)              task_complete gate: verify + required files + must_contain
  ReasoningBudget                       runaway-reasoning (no tool call) detector
  extract_bash_block(content)           bash-only action mode (mini-swe-agent style)

Patterns: SWE-agent forward_with_handling (templated error re-prompt, max_requeries=3);
mini-swe-agent (RepeatedFormatError / named exit reasons); goose toolshim (layered extraction);
vLLM qwen3_coder + hermes tool parsers; oh-my-agent persistent-mode (stop-gate re-checks ALL
criteria every round); Tianshu convergence-detector (refuse 'done' without evidence).
"""
import hashlib
import json
import os
import re

# ---- named exit reasons (registered in failure_ledger.py + worker terminal reasons) -------
REASON_FORMAT = "repeated_format_error"
REASON_LOOP = "loop_detected"
REASON_STOPGATE = "stop_gate_failed"
REASON_REASONING = "reasoning_runaway"
# loop-detector sub-kinds that end the run under their OWN named reason (2026-10-09; stolen from
# OpenHands' stuck detector / SWE-agent navigation limits). The legacy kinds (repeat_read,
# repeat_write, write_thrash, verify_spin, no_progress) still end as REASON_LOOP.
REASON_ERROR_LOOP = "error_loop"            # same action + same error, 4th time
REASON_MONOLOGUE = "monologue_loop"         # 3 consecutive assistant turns with no tool call
REASON_ALTERNATION = "alternation_loop"     # A,B,A,B,... alternation
REASON_NAV_LOOP = "nav_loop"                # endless grep/find/ls/view/read_file navigation
LOOP_KIND_REASONS = {"error_streak": REASON_ERROR_LOOP, "monologue": REASON_MONOLOGUE,
                     "alternation": REASON_ALTERNATION, "nav_streak": REASON_NAV_LOOP}
# BOUNDED READ-ONLY REVIEW JOBS (2026-10-09, esc-review 2ac42be77bcc: 8 iterations re-reading the
# same two tiny files, a `list_files .` of 2149 entries, then one unbounded 1500+-line generation).
# A research task that captures its final answer to a file (an escalation review) gets three hard
# bounds, each ending the run under a NAMED reason and a captured "REVIEW CAPPED" marker file:
REASON_REPEAT_CALL = "repeat_call_loop"     # the same read-only call re-issued (2x in a row / 3x total)
REASON_REVIEW_CAP = "output_cap_review"     # a turn hit the (explicit, small) output-token cap
# SAMPLING ESCALATION LADDER (A/B arm, default OFF; model_profiles.yaml `sampling_escalation`).
REASON_SPEC_DEFECT_REPEAT = "spec_defect_repeat_abort"   # 2 aborts on the SAME failing check: spec path first
REASON_LADDER_EXHAUSTED = "sampling_ladder_exhausted"    # step 3: still looping -> scheduler re-specs
# Reasoning-loop recovery exhausted (plain retry looped, thinking-off rung looped): with the profile flag
# robust.loop_exhaust_exit (default OFF) the run ENDS under this named reason instead of parking paused.
REASON_LOOP_EXHAUSTED = "reasoning_loop_exhausted"
# PER-TURN FAN-OUT (job c8f4f6ed95c1, 2026-10-09: ONE turn emitted 582 read_file calls, offset +100 past
# EOF, 328s of decode; the run then ended as a generic nav_loop). Applies to EVERY task kind.
REASON_TOOL_FANOUT = "tool_fanout_loop"       # a turn emitted more than the per-turn cap of tool calls, repeatedly
REASON_READ_PAST_EOF = "read_past_eof_loop"   # consecutive read_file calls whose offset is past end of file
NEW_EXIT_REASONS = (REASON_FORMAT, REASON_LOOP, REASON_STOPGATE, REASON_REASONING,
                    REASON_ERROR_LOOP, REASON_MONOLOGUE, REASON_ALTERNATION, REASON_NAV_LOOP,
                    REASON_REPEAT_CALL, REASON_REVIEW_CAP, REASON_TOOL_FANOUT, REASON_READ_PAST_EOF,
                    REASON_SPEC_DEFECT_REPEAT, REASON_LADDER_EXHAUSTED, REASON_LOOP_EXHAUSTED)
REVIEW_REPEAT_CONSECUTIVE = 2   # the identical read-only call twice in a row ends the run
REVIEW_REPEAT_TOTAL = 3         # ... or the third time overall (A,B,A,B,A re-reading evades "in a row")
LIST_FILES_MAX_ENTRIES = 400    # list_files refuses a directory bigger than this (lists a sample)


def review_bounds_active(task_kind, capture_final_as):
    """PURE. The bounds above apply to research tasks that capture a final answer to a file."""
    return task_kind == "research" and bool(capture_final_as)


def repeat_call_key(name, args, resolve=None):
    """PURE. The normalised identity of a read-only inspection (read_file / list_files), or None
    for any other tool. read_file ignores `length` (a model varying length/offset spelling of the
    same page is the same read); the path is resolved so `x`, `./x` and the absolute path match."""
    if name not in ("read_file", "list_files") or not isinstance(args, dict):
        return None
    path = str(args.get("path") or ".")
    if resolve is not None:
        try:
            path = str(resolve(path))
        except Exception:
            pass
    if name == "read_file":
        try:
            off = int(args.get("offset") or 1)
        except (TypeError, ValueError):
            off = 1
        return (name, path, off)
    return (name, path)


class RepeatCallGuard:
    """observe(key) -> None, or a human detail string when the call has been repeated too often."""

    def __init__(self, consecutive=REVIEW_REPEAT_CONSECUTIVE, total=REVIEW_REPEAT_TOTAL):
        self.consecutive, self.total = consecutive, total
        self.counts, self.last, self.run = {}, None, 0

    def observe(self, key):
        if key is None:
            self.last, self.run = None, 0
            return None
        self.counts[key] = self.counts.get(key, 0) + 1
        self.run = self.run + 1 if key == self.last else 1
        self.last = key
        if self.run >= self.consecutive:
            return "%s re-issued %d times in a row" % (" ".join(map(str, key)), self.run)
        if self.counts[key] >= self.total:
            return "%s issued %d times in total" % (" ".join(map(str, key)), self.counts[key])
        return None


class TurnFanoutGuard:
    """PURE. Per-turn tool-call cap + consecutive past-EOF read counter, for ALL task kinds.

    on_turn(n_calls): None while n_calls <= cap. Over the cap: the 1st offence -> ("warn", reason,
      message) (the excess is discarded by the caller and the model is told why); the `max_offences`-th
      -> ("stop", REASON_TOOL_FANOUT, detail).
    on_call(name, past_eof): called for each EXECUTED call. A past-EOF read_file extends a consecutive
      streak (warn at `eof_warn`, stop at `eof_stop` -> REASON_READ_PAST_EOF); any other call resets it."""

    def __init__(self, cap=12, max_offences=2, eof_warn=3, eof_stop=6):
        self.cap, self.max_offences = int(cap), int(max_offences)
        self.eof_warn, self.eof_stop = int(eof_warn), int(eof_stop)
        self.offences = 0
        self.eof_streak = 0

    def on_turn(self, n_calls):
        if n_calls <= self.cap:
            return None
        self.offences += 1
        detail = ("a single turn emitted %d tool calls (cap %d); the extra %d were discarded"
                  % (n_calls, self.cap, n_calls - self.cap))
        if self.offences >= self.max_offences:
            return ("stop", REASON_TOOL_FANOUT, "%s -- offence #%d" % (detail, self.offences))
        return ("warn", REASON_TOOL_FANOUT,
                "Your last turn contained %d tool calls; only the first %d were run and the rest were "
                "DISCARDED. Make at most %d tool calls per turn, one step at a time, and look at each "
                "result before deciding the next call. Do not page through a file with many offsets "
                "in one turn." % (n_calls, self.cap, self.cap))

    def on_call(self, name, past_eof=False):
        if name == "read_file" and past_eof:
            self.eof_streak += 1
            if self.eof_streak >= self.eof_stop:
                return ("stop", REASON_READ_PAST_EOF,
                        "%d consecutive read_file calls with an offset past end of file" % self.eof_streak)
            if self.eof_streak == self.eof_warn:
                return ("warn", REASON_READ_PAST_EOF,
                        "Your last %d read_file calls used an offset past the END of the file. The "
                        "file is shorter than you think: re-read it from offset 1 (or grep for the "
                        "line you want) instead of stepping the offset forward." % self.eof_streak)
        else:
            self.eof_streak = 0
        return None


def is_past_eof_result(result):
    """PURE. True for tool_read_file's 'offset N is past the end of ...' refusal."""
    return isinstance(result, str) and result.startswith("ERROR: offset ") and "past the end of" in result[:200]


def review_capped_text(reason, detail, iteration=None):
    """PURE. The text captured as the review when a bound ended the run. FIRST LINE is the marker
    escalation_verdict.is_no_verdict_review() recognises (consumers must NOT act on it). No partial
    model output is included: a runaway's tail is junk and could contain verdict-looking text."""
    return ("REVIEW CAPPED (%s) -- the review stopped by a worker bound before giving a verdict%s: %s. "
            "No model verdict exists; the escalation stays open and nothing was acted on.\n"
            % (reason, (" at iteration %s" % iteration) if iteration else "", detail))


def loop_exit_reason(kind):
    """The named exit reason a loop-detector `kind` ends the run with."""
    return LOOP_KIND_REASONS.get(kind, REASON_LOOP)

# ---------------------------------------------------------------------------------------
# 0. think-tag handling
# ---------------------------------------------------------------------------------------
_THINK_CLOSED_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think(?:ing)?>", re.IGNORECASE)
_THINK_STRAY_CLOSE_RE = re.compile(r"</think(?:ing)?>", re.IGNORECASE)


def strip_think(text):
    """Remove reasoning that leaked into content. Returns (visible, info) where info is
    {"closed": n, "unterminated": bool, "stray_close": bool}.
      * closed <think>..</think> blocks are dropped (a call QUOTED inside reasoning is not a call)
      * an UNTERMINATED <think> (generation cut mid-reasoning) drops everything after the opener
      * a stray </think> with no opener (template put the opener in the prompt) drops everything
        up to and including it -- what precedes it is reasoning."""
    text = text or ""
    info = {"closed": 0, "unterminated": False, "stray_close": False}
    out, n = _THINK_CLOSED_RE.subn("", text)
    info["closed"] = n
    m = _THINK_OPEN_RE.search(out)
    if m:
        info["unterminated"] = True
        out = out[:m.start()]
    m = _THINK_STRAY_CLOSE_RE.search(out)
    if m:
        info["stray_close"] = True
        out = out[m.end():]
    return out.strip(), info


# ---------------------------------------------------------------------------------------
# 1. layered tool-call repair
# ---------------------------------------------------------------------------------------
_READONLY_OK_TRUNCATED = frozenset({"read_file", "list_files"})
_XML_OPEN_RE = re.compile(r"<function=([\w.\-]+)>")
_PARAM_OPEN_RE = re.compile(r"<parameter=([\w.\-]+)>")


def _trim_one_newline(v):
    if v.startswith("\n"):
        v = v[1:]
    if v.endswith("\n"):
        v = v[:-1]
    return v


def _coerce_param(key, value):
    if key == "additional":
        try:
            return int(value.strip())
        except ValueError:
            return value
    return value


def parse_qwen3_xml(content, valid_names):
    """Qwen3-Coder XML: <function=NAME><parameter=K>V</parameter>...</function>, optionally inside
    <tool_call>..</tool_call>. Tolerates a MISSING </parameter>, </function> and </tool_call>
    (the vLLM qwen3_coder parser does the same for streamed/unterminated output).
    Returns (calls, spans, truncated) -- spans are (start, end) of what was consumed;
    truncated is True when the LAST call ran to end-of-input with no closer at all (so its last
    parameter value may be cut short)."""
    calls, spans, truncated = [], [], False
    pos = 0
    while True:
        m = _XML_OPEN_RE.search(content, pos)
        if not m:
            break
        name = m.group(1)
        body_start = m.end()
        # the call ends at </function>, else at </tool_call>, else at the next <function=, else EOF
        end_f = content.find("</function>", body_start)
        nxt = _XML_OPEN_RE.search(content, body_start)
        end_t = content.find("</tool_call>", body_start)
        cands = [(p, ln) for p, ln in ((end_f, len("</function>")), (end_t, 0)) if p >= 0]
        if nxt:
            cands.append((nxt.start(), 0))
        if cands:
            body_end, closer_len = min(cands)
            closed = True
        else:
            body_end, closer_len, closed = len(content), 0, False
        body = content[body_start:body_end]
        args = {}
        ppos = 0
        while True:
            pm = _PARAM_OPEN_RE.search(body, ppos)
            if not pm:
                break
            key = pm.group(1)
            vstart = pm.end()
            vend = body.find("</parameter>", vstart)
            nextp = _PARAM_OPEN_RE.search(body, vstart)
            if vend >= 0 and (not nextp or vend < nextp.start()):
                value, ppos = body[vstart:vend], vend + len("</parameter>")
            elif nextp:                      # missing </parameter>: value runs to the next <parameter=
                value, ppos = body[vstart:nextp.start()], nextp.start()
            else:                            # last parameter, no closer: runs to the end of the call
                value, ppos = body[vstart:], len(body)
            args[key] = _coerce_param(key, _trim_one_newline(value))
        # extend the consumed span over a trailing </tool_call> and a leading <tool_call>
        s, e = m.start(), body_end + closer_len
        lead = content[:s].rstrip()
        if lead.lower().endswith("<tool_call>"):
            s = len(lead) - len("<tool_call>")
        tail = content[e:]
        tm = re.match(r"\s*</tool_call>", tail)
        if tm:
            e += tm.end()
        if name in valid_names:
            calls.append({"name": name, "arguments": args})
            spans.append((s, e))
            if not closed:
                truncated = True
        pos = max(e, body_start)
    return calls, spans, truncated


def _balance_json(s):
    """Close an unterminated JSON object: finish an open string (dropping a dangling backslash),
    drop a dangling comma/colon, close open brackets/braces. Returns repaired text or None."""
    stack, in_str, esc = [], False, False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack:
                return None
            stack.pop()
    if not stack and not in_str:
        return s
    out = s
    if in_str:
        if esc:
            out = out[:-1]
        out += '"'
    out = re.sub(r"[,:]\s*$", "", out)
    # a key with no value left over (`..., "key"`) -> give it null so json.loads accepts it
    if re.search(r'[{,]\s*"[^"\\]*"\s*$', out) and stack and stack[-1] == "}":
        out += ": null"
    return out + "".join(reversed(stack))


def _escape_ctrl(s):
    res, in_str, esc = [], False, False
    for ch in s:
        if not in_str:
            res.append(ch)
            in_str = ch == '"'
            continue
        if esc:
            res.append(ch)
            esc = False
        elif ch == "\\":
            res.append(ch)
            esc = True
        elif ch == '"':
            res.append(ch)
            in_str = False
        else:
            res.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(ch, ch))
    return "".join(res)


def _loads_lenient(s):
    for fix in (lambda x: x, _escape_ctrl,
                lambda x: re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", x),
                lambda x: _escape_ctrl(re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", x))):
        try:
            return json.loads(fix(s))
        except Exception:
            continue
    return None


def _json_to_call(obj, valid_names):
    if not isinstance(obj, dict):
        return None
    fn = obj.get("function") if isinstance(obj.get("function"), dict) else None
    if fn is not None:
        obj = fn
    name = obj.get("name") or obj.get("tool") or obj.get("tool_name")
    if not isinstance(name, str) or name.strip() not in valid_names:
        return None
    args = obj.get("arguments", obj.get("parameters"))
    if isinstance(args, str):
        args = _loads_lenient(args) or {}
    if args is None and "name" in obj:
        args = {}
    if not isinstance(args, dict):
        args = {k: v for k, v in obj.items() if k not in ("name", "tool", "tool_name", "function")}
    return {"name": name.strip(), "arguments": args}


def parse_hermes_json(content, valid_names):
    """Hermes: <tool_call>{"name":..,"arguments":{..}}</tool_call>. Tolerates trailing text after
    the JSON, a missing </tool_call>, and a JSON object cut off mid-string (balanced + closed).
    Returns (calls, spans, truncated)."""
    calls, spans, truncated = [], [], False
    for m in re.finditer(r"<tool_call>\s*", content):
        start = m.end()
        if content[start:start + 9].startswith("<function"):
            continue                          # XML dialect, handled by parse_qwen3_xml
        brace = content.find("{", start)
        if brace < 0 or content[start:brace].strip():
            continue
        # string-aware scan for the matching close
        depth, in_str, esc, end = 0, False, False, None
        for i in range(brace, len(content)):
            ch = content[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        trunc = end is None
        if trunc:
            close = content.find("</tool_call>", brace)
            cand = content[brace:close] if close >= 0 else content[brace:]
            cand = _balance_json(cand)
            if cand is None:
                continue
            seg_end = close + len("</tool_call>") if close >= 0 else len(content)
        else:
            cand = content[brace:end]
            seg_end = end
            tm = re.match(r"\s*</tool_call>", content[end:])
            if tm:
                seg_end += tm.end()
        call = _json_to_call(_loads_lenient(cand), valid_names)
        if call:
            calls.append(call)
            spans.append((m.start(), seg_end))
            truncated = truncated or trunc
    return calls, spans, truncated


def parse_inline_json(content, valid_names):
    """Bare {"name": N, "arguments": {...}} (or {"tool": N, ...}) anywhere in the text, string-aware.
    Returns (calls, spans, truncated=False). Complements the worker's regex scan: this one also
    accepts a name that is a known tool only (prose false positives), nested "function" wrappers
    and string-encoded arguments."""
    calls, spans = [], []
    i, n = 0, len(content)
    while i < n:
        j = content.find("{", i)
        if j < 0:
            break
        depth, in_str, esc, end = 0, False, False, None
        for k in range(j, n):
            ch = content[k]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = k + 1
                    break
        if end is None:
            i = j + 1
            continue
        obj = _loads_lenient(content[j:end])
        call = _json_to_call(obj, valid_names) if obj is not None else None
        if call and any(k in obj for k in ("name", "tool", "tool_name", "function")):
            calls.append(call)
            spans.append((j, end))
            i = end
        else:
            i = j + 1
    return calls, spans, False


def _blank_spans(content, spans):
    out = content
    for s, e in sorted(spans, reverse=True):
        out = out[:s] + out[e:]
    return out.strip()


def repair_tool_calls(content, valid_names, tool_schemas=None, extractor=None):
    """Layered extraction. Returns dict:
      calls    : [{"name","arguments"}]   (possibly [])
      cleaned  : content with the consumed call text removed
      layer    : "xml" | "hermes" | "inline" | "extractor" | None
      truncated: True when an accepted call ran to end-of-input with no closer
      refused_truncated: [names] of truncated calls NOT accepted (a half-written write_file must
                         never be executed -- the caller re-prompts instead)
      think    : strip_think info
    `extractor(visible_text) -> list[call]` is the optional small-model fallback (profile flag
    `tool_extractor`, off by default); it only runs when every deterministic layer found nothing
    AND the text looks like a call attempt."""
    valid = set(valid_names)
    visible, think = strip_think(content)
    res = {"calls": [], "cleaned": visible, "layer": None, "truncated": False,
           "refused_truncated": [], "think": think}
    if not visible:
        return res
    for layer, fn in (("xml", parse_qwen3_xml), ("hermes", parse_hermes_json),
                      ("inline", parse_inline_json)):
        calls, spans, trunc = fn(visible, valid)
        if not calls:
            continue
        if trunc:
            keep = []
            for c in calls:
                if c["name"] in _READONLY_OK_TRUNCATED:
                    keep.append(c)
                else:
                    res["refused_truncated"].append(c["name"])
            if len(keep) != len(calls):
                calls = keep
        if calls:
            res.update(calls=calls, cleaned=_blank_spans(visible, spans), layer=layer, truncated=trunc)
            return res
        if res["refused_truncated"]:
            return res
    if extractor is not None and looks_like_call_attempt(visible, valid):
        try:
            calls = [c for c in (extractor(visible) or []) if isinstance(c, dict)
                     and c.get("name") in valid and isinstance(c.get("arguments", {}), dict)]
        except Exception:
            calls = []
        if calls:
            res.update(calls=calls, layer="extractor")
    return res


# ---------------------------------------------------------------------------------------
# 2. format-error diagnosis + requery
# ---------------------------------------------------------------------------------------
def looks_like_call_attempt(visible, valid_names=()):
    """True when prose-free-ish content smells like a tool call: tags, a json fence with name/args,
    or a JSON object naming a tool."""
    if re.search(r"</?tool_call>|<function=|<parameter=|\[TOOL_CALLS\]|</?function>", visible):
        return True
    if re.search(r'"(?:name|tool|tool_name)"\s*:\s*"(?:%s)"' % "|".join(map(re.escape, valid_names or ["\0"])), visible):
        return True
    if re.search(r'"arguments"\s*:\s*\{', visible):
        return True
    return False


# Parameters the schema marks REQUIRED (so the model is asked to always supply them -- optional
# params make Qwen3.5-35B / Qwen3-Coder loop at long contexts, llama.cpp #20164) but whose OMISSION
# is still honoured with this default, so old transcripts / weaker models are not format-errored.
DEFAULTED_PARAMS = {"read_file": {"offset": 1, "length": 0}, "request_diagnostics": {"why": ""}}


def required_params(tool_schemas):
    out = {}
    for t in tool_schemas or []:
        fn = t.get("function", t)
        name = fn.get("name")
        out[name] = [k for k in ((fn.get("parameters") or {}).get("required") or [])
                     if k not in DEFAULTED_PARAMS.get(name, {})]
    return out


def validate_calls(calls, tool_schemas):
    """Missing-required-parameter problems for parsed calls -> list of (name, [missing])."""
    req = required_params(tool_schemas)
    bad = []
    for c in calls:
        need = req.get(c["name"])
        if need is None:
            continue
        args = c.get("arguments") or {}
        miss = [k for k in need if k not in args]
        if miss:
            bad.append((c["name"], miss))
    return bad


FORMAT_EXAMPLE = ("<tool_call>\n<function=NAME>\n<parameter=KEY>\nVALUE\n</parameter>\n"
                  "</function>\n</tool_call>")


def diagnose_format_error(content, valid_names, tool_schemas=None, repair=None):
    """None when `content` is not a failed tool-call attempt. Otherwise (kind, message) with a
    templated, specific description of the problem. `repair` is a repair_tool_calls() result for
    the same content (computed if omitted). Kinds: truncated, unknown_tool, bad_json,
    missing_param, dangling_closer, no_parseable_call."""
    valid = sorted(set(valid_names))
    repair = repair or repair_tool_calls(content, valid)
    visible = strip_think(content)[0]
    if repair["calls"]:
        bad = validate_calls(repair["calls"], tool_schemas)
        if bad:
            nm, miss = bad[0]
            return ("missing_param",
                    f"Your {nm} call is missing required parameter(s): {', '.join(miss)}. "
                    f"Re-send the call with every required parameter.")
        return None
    if repair["refused_truncated"]:
        nm = repair["refused_truncated"][0]
        return ("truncated",
                f"Your {nm} call was cut off before it was closed (no closing tag / unterminated "
                f"value), so it was NOT run -- a half-written file must not be applied. Re-send it "
                f"complete. If the content is long, write the file in smaller pieces (create it "
                f"with the first part, then extend it with edit_file or a shell append).")
    if not looks_like_call_attempt(visible, valid):
        return None
    m = _XML_OPEN_RE.search(visible)
    if m and m.group(1) not in valid:
        return ("unknown_tool",
                f"'{m.group(1)}' is not a tool. Valid tools: {', '.join(valid)}. "
                f"Use exactly one of those names.")
    for jm in re.finditer(r'"(?:name|tool|tool_name)"\s*:\s*"([^"]+)"', visible):
        if jm.group(1) not in valid:
            return ("unknown_tool",
                    f"'{jm.group(1)}' is not a tool. Valid tools: {', '.join(valid)}. "
                    f"Use exactly one of those names.")
    if re.search(r"</tool_call>", visible) and not re.search(r"<tool_call>", visible):
        return ("dangling_closer",
                "Your response has a closing </tool_call> but no opening <tool_call>, so no call "
                "was recognized. Open the block with <tool_call> before the <function=...> line.")
    if "{" in visible and re.search(r'"(?:name|arguments)"', visible):
        return ("bad_json",
                "Your tool call contains JSON that could not be parsed (check quotes, commas and "
                "escaping of newlines inside strings). Re-send it as valid JSON, or use the "
                "XML format.")
    return ("no_parseable_call",
            "A tool call was started but could not be parsed. Use exactly this format and "
            "nothing else:\n" + FORMAT_EXAMPLE)


def format_error_message(kind, detail, attempt, limit):
    if kind == "bash_format":
        return f"[format error {attempt}/{limit}] {detail}"
    return (f"[format error {attempt}/{limit}] {detail}\n"
            f"Reply with ONE corrected tool call using exactly this format:\n{FORMAT_EXAMPLE}")


def make_llm_extractor(chat_fn, valid_names):
    """Optional last repair layer (profile `tool_extractor`, OFF by default): ask a model to
    re-emit the tool call(s) as JSON. `chat_fn(messages) -> str`. Only called when every
    deterministic layer found nothing AND the text looks like a call attempt."""
    names = sorted(valid_names)

    def extract(text):
        out = chat_fn([
            {"role": "system", "content": "You convert a malformed tool call into strict JSON. Reply with "
             'ONLY {"name": <tool>, "arguments": {...}} or the single word NONE. Valid tools: '
             + ", ".join(names) + ". Never invent arguments that are not in the text."},
            {"role": "user", "content": text[-6000:]}])
        calls, _, _ = parse_inline_json(out or "", set(names))
        return calls
    return extract


class FormatRequery:
    """Consecutive format errors are re-prompted for free (not charged as an iteration) up to
    `limit` times; the next one ends the run with REASON_FORMAT. Any good turn resets."""

    def __init__(self, limit=3):
        self.limit = int(limit)
        self.streak = 0
        self.total = 0
        self.kinds = []

    def on_error(self, kind):
        """-> ("requery", n) or ("stop", n)"""
        self.streak += 1
        self.total += 1
        self.kinds.append(kind)
        if self.streak > self.limit:
            return "stop", self.streak
        return "requery", self.streak

    def on_ok(self):
        self.streak = 0


# ---------------------------------------------------------------------------------------
# 3. loop / convergence detector
# ---------------------------------------------------------------------------------------
LOOP_DEFAULTS = {
    "repeat_read": 5,       # identical read-only call this many times
    "repeat_write": 4,      # identical write (same path, same bytes) this many times
    "aba_returns": 3,       # times a path returns to an EARLIER content hash (A->B->A counts 1)
    "no_progress_iters": 8,  # consecutive iterations with nothing novel (no new call sig, no new bytes)
    "verify_spin": 0,       # OFF by default (poor precision, see calibration); the job's own verify re-run this many times in a row with NO edit between
    "min_iter": 4,          # never fire before this iteration
    # --- 2026-10-09 additions (each 0 = off) ---
    "error_streak_warn": 3,   # same call + same error this many times in a row: ONE nudge ...
    "error_streak_stop": 4,   # ... and stop on this one (error_loop)
    "monologue": 3,           # consecutive assistant turns with no tool call (monologue_loop)
    "alternation": 6,         # A,B,A,B,... over this many calls: warn; stop after `alternation_stop`
    "alternation_stop": 8,
    "nav_warn": 15,           # consecutive navigation calls (grep/find/ls/view/read_file as ONE class)
    "nav_stop_after": 8,      # ... stop this many navigation calls after the warning (nav_loop)
}
_READ_TOOLS = frozenset({"read_file", "list_files", "web_fetch", "web_search"})
_RO_BASH_RE = re.compile(
    r"^\s*(?:cd\s+\S+\s*&&\s*)?(?:ls|cat|head|tail|grep|egrep|rg|wc|which|pwd|echo|stat|file|find|diff|"
    r"sed\s+-n|git\s+(?:log|status|diff|show|branch|rev-parse|ls-files)|python3?\s+-c|node\s+-e)\b")


def bash_is_readonly(cmd):
    """Conservative: a pipeline of read-only commands with no redirection/in-place flag."""
    cmd = str(cmd or "")
    if re.search(r"(?<![<2&])>|>>|\btee\b|\bsed\s+-i|\brm\b|\bmv\b|\bcp\b|\bmkdir\b|<<", cmd):
        return False
    return all(_RO_BASH_RE.match(part) for part in re.split(r"\s*(?:&&|\|\||;|\|)\s*", cmd.strip()) if part)
_WRITE_TOOLS = frozenset({"write_file", "edit_file"})


def loop_config(profile_robust=None, env=None):
    """defaults <- profile `robust.loop_detector` <- env WORKER_LOOP_<KEY> (int)."""
    cfg = dict(LOOP_DEFAULTS)
    cfg["enabled"] = True
    p = (profile_robust or {}).get("loop_detector") or {}
    for k, v in p.items():
        if k in cfg or k == "enabled":
            cfg[k] = v
    env = os.environ if env is None else env
    for k in list(cfg):
        ev = env.get("WORKER_LOOP_" + k.upper())
        if ev is not None and ev != "":
            try:
                cfg[k] = bool(int(ev)) if k == "enabled" else int(ev)
            except ValueError:
                pass
    if env.get("READONLY_JOB") == "1":      # read-only investigation: "navigation with no edit" is the job
        cfg["nav_warn"] = 0
    return cfg


def _h(s):
    return hashlib.sha1(str(s).encode("utf-8", "replace")).hexdigest()[:12]


_NAV_CMD_RE = re.compile(
    r"^\s*(?:cd\s+\S+\s*&&\s*)?(?:grep|egrep|fgrep|rg|ag|ack|find|fd|ls|tree|cat|cd|head|tail|less|more|"
    r"view|bat|wc|stat|file|pwd|git\s+(?:grep|ls-files|log|show|diff|status))\b")
_NAV_TOOLS = frozenset({"read_file", "list_files", "view", "grep", "find", "ls", "search_files"})


def is_navigation_call(name, args):
    """grep/find/ls/view/read_file/list_files and read-only shell lookups are ONE class: a
    streak of them with no edit is exploration that never ends."""
    if name in _NAV_TOOLS:
        return True
    if name == "run_bash":
        cmd = str((args or {}).get("command") or "")
        parts = [p for p in re.split(r"\s*(?:&&|\|\||;|\|)\s*", cmd.strip()) if p]
        return bool(parts) and not re.search(r"(?<![<2&])>|>>|\btee\b|<<", cmd) and \
            all(_NAV_CMD_RE.match(p) for p in parts)
    return False


def normalise_error(text):
    """Collapse an error to a comparable key (first line, digits/hex squashed)."""
    t = str(text or "").strip()
    line = t.splitlines()[0] if t else ""
    line = re.sub(r"0x[0-9a-fA-F]+|\b\d+\b", "N", line)
    return line[:160]


class LoopDetector:
    """Feed it every tool call (observe) and tell it when an iteration ends (end_iteration).
    end_iteration returns None, or (action, kind, message) with action "warn" (first detection,
    inject message) or "stop" (second detection -> REASON_LOOP)."""

    def __init__(self, cfg=None):
        self.cfg = dict(LOOP_DEFAULTS)
        self.cfg["enabled"] = True
        self.cfg.update(cfg or {})
        self.read_counts = {}
        self.write_counts = {}
        self.path_hist = {}        # path -> list of content hashes (collapsed consecutive dupes)
        self.path_returns = {}     # path -> number of returns to an earlier hash
        self.seen_sigs = set()
        self.iter_novel = False
        self.stale_iters = 0
        self.novel_writes = 0      # writes with content never seen before on that path
        self.warn_novel_writes = None
        self.mutations = 0         # edits / non-read-only shell commands so far
        self.verify_spin_n = 0
        self._last_verify_mut = None
        self.warned = False
        self.detections = []
        self._calls_this_iter = 0
        # 2026-10-09 detectors (own state; per-kind one-time warning)
        self.err_sig = None        # (call sig, normalised error) of the last failing call
        self.err_streak = 0
        self.sig_hist = []         # recent call sigs (non-exempt), for A,B,A,B alternation
        self.nav_streak = 0
        self.nav_warn_at = None
        self.no_tool_streak = 0
        self.kind_warned = set()

    # -- input ----------------------------------------------------------------------------
    def observe(self, name, args, exempt=False, content_hash=None, error=None):
        """exempt: the job's own verify command (re-running it after an edit is progress).
        error: the tool's error text when the call failed (drives the same-error streak)."""
        if not self.cfg.get("enabled"):
            return
        args = args if isinstance(args, dict) else {}
        self._calls_this_iter += 1
        sig = name + ":" + _h(json.dumps(args, sort_keys=True, default=str))
        if not exempt:
            self._observe_extra(name, args, sig, error)
        if sig not in self.seen_sigs:
            self.seen_sigs.add(sig)
            if not exempt:
                self.iter_novel = True
        if exempt:
            # the job's own verify: progress only if something changed since its last run
            if self._last_verify_mut is not None and self._last_verify_mut == self.mutations:
                self.verify_spin_n += 1
            else:
                self.verify_spin_n = 0
            self._last_verify_mut = self.mutations
            return
        if name in _WRITE_TOOLS or (name == "run_bash" and not bash_is_readonly(args.get("command"))):
            self.mutations += 1
            self.read_counts.clear()    # state changed: re-reading is no longer "the same thing"
            if name == "run_bash" and self.read_counts.get(sig, 0) == 0:
                self.novel_writes += 1      # a new mutating shell command (heredoc write etc.)
        if name in _WRITE_TOOLS or (name == "run_bash" and content_hash):
            path = str(args.get("path") or "")
            if name == "edit_file":
                ch = _h("edit|" + str(args.get("old_string")) + "|" + str(args.get("new_string")))
            elif content_hash:
                ch = content_hash
            else:
                ch = _h(args.get("content"))
            key = (path, ch)
            self.write_counts[key] = self.write_counts.get(key, 0) + 1
            hist = self.path_hist.setdefault(path, [])
            if not hist or hist[-1] != ch:
                if ch in hist:
                    self.path_returns[path] = self.path_returns.get(path, 0) + 1
                if ch not in hist:
                    self.novel_writes += 1
                    self.iter_novel = True
                hist.append(ch)
        elif name in _READ_TOOLS or name == "run_bash":
            self.read_counts[sig] = self.read_counts.get(sig, 0) + 1


    # -- 2026-10-09 detectors -------------------------------------------------------------
    def _observe_extra(self, name, args, sig, error):
        c = self.cfg
        # same action + same ERROR: consecutive; a success or any different call breaks it
        if error:
            # the worker's repeated-failure hard block answers the Nth identical failing call with a
            # "REFUSED: ..." instead of the original error -- same call, so it continues the streak
            refused = str(error).startswith("REFUSED")
            key = (sig, normalise_error(error))
            same = key == self.err_sig or (refused and self.err_sig is not None and self.err_sig[0] == sig)
            self.err_streak = self.err_streak + 1 if same else 1
            self.err_sig = self.err_sig if (same and refused) else key
        else:
            self.err_sig, self.err_streak = None, 0
        # A,B,A,B alternation over the last N non-exempt calls
        self.sig_hist.append(sig)
        keep = max(int(c.get("alternation_stop") or 0), int(c.get("alternation") or 0), 2)
        if len(self.sig_hist) > keep:
            del self.sig_hist[:-keep]
        # navigation class: grep/find/ls/view/read_file/list_files are one thing; anything else resets
        if is_navigation_call(name, args):
            self.nav_streak += 1
        else:
            self.nav_streak = 0
            self.nav_warn_at = None

    def observe_turn(self, has_tool_call):
        """Feed one assistant turn: consecutive turns with NO tool call are a monologue."""
        if not self.cfg.get("enabled"):
            return
        self.no_tool_streak = 0 if has_tool_call else self.no_tool_streak + 1

    def monologue_verdict(self):
        """Call at the top of each iteration (a turn that ended the run never gets here).
        -> None | ("stop", "monologue", msg)."""
        n = int(self.cfg.get("monologue") or 0)
        if self.cfg.get("enabled") and n and self.no_tool_streak >= n:
            self.detections.append("monologue")
            return ("stop", "monologue", f"loop detected: {self.no_tool_streak} consecutive assistant "
                    f"turns made no tool call (the run kept being nudged and kept talking)")
        return None

    def _alternating(self, n):
        h = self.sig_hist
        if n < 4 or len(h) < n:
            return False
        w = h[-n:]
        a, b = w[0], w[1]
        return a != b and all(w[k] == (a if k % 2 == 0 else b) for k in range(n))

    def _detect_extra(self):
        """-> None | (action, kind, what). Per-kind ONE-time warning, then stop."""
        c = self.cfg
        sw, ss = int(c.get("error_streak_warn") or 0), int(c.get("error_streak_stop") or 0)
        if self.err_sig and ss and self.err_streak >= ss:
            return ("stop", "error_streak", f"the same call has failed with the same error "
                    f"{self.err_streak} times in a row")
        if self.err_sig and sw and self.err_streak >= sw and "error_streak" not in self.kind_warned:
            self.kind_warned.add("error_streak")
            return ("warn", "error_streak", f"you have made the same call {self.err_streak} times in a "
                    f"row and gotten the same error each time")
        na, ns = int(c.get("alternation") or 0), int(c.get("alternation_stop") or 0)
        if ns and self._alternating(ns):
            return ("stop", "alternation", f"the last {ns} calls alternate between the same two calls "
                    f"(A,B,A,B,...) and neither moves the task")
        if na and self._alternating(na) and "alternation" not in self.kind_warned:
            self.kind_warned.add("alternation")
            return ("warn", "alternation", f"the last {na} calls alternate between the same two calls "
                    f"(A,B,A,B,...)")
        nw, nst = int(c.get("nav_warn") or 0), int(c.get("nav_stop_after") or 0)
        if nw and self.nav_streak >= nw:
            if self.nav_warn_at is None:
                self.nav_warn_at = self.nav_streak
                return ("warn", "nav_streak", f"the last {self.nav_streak} calls were all navigation "
                        f"(grep/find/ls/view/read_file) with no edit")
            if nst and self.nav_streak >= self.nav_warn_at + nst:
                return ("stop", "nav_streak", f"{self.nav_streak} navigation calls in a row "
                        f"(grep/find/ls/view/read_file) and still no edit")
        return None

    # -- verdict --------------------------------------------------------------------------
    def _detect(self, i):
        c = self.cfg
        if i < c["min_iter"]:
            return None
        for sig, n in self.read_counts.items():
            if n >= c["repeat_read"]:
                return ("repeat_read", sig, f"the same call ({sig.split(':')[0]}) has now been issued "
                        f"{n} times with identical arguments and returns the same thing")
        for (path, _), n in self.write_counts.items():
            if n >= c["repeat_write"]:
                return ("repeat_write", path, f"you have written identical content to {path or 'a file'} "
                        f"{n} times")
        for path, n in self.path_returns.items():
            if n >= c["aba_returns"]:
                return ("write_thrash", path, f"{path or 'a file'} keeps flipping back to content it already "
                        f"had (A->B->A, {n} reversions)")
        if c.get("verify_spin") and self.verify_spin_n + 1 >= c["verify_spin"]:
            return ("verify_spin", "", f"the verify command has now run {self.verify_spin_n + 1} times in a "
                    f"row with no edit in between, so it can only report the same failure")
        if self.stale_iters >= c["no_progress_iters"]:
            return ("no_progress", "", f"the last {self.stale_iters} iterations produced nothing new "
                    f"(no new command, no new file content)")
        return None

    def end_iteration(self, i):
        if not self.cfg.get("enabled"):
            return None
        if self.iter_novel or self._calls_this_iter == 0:
            self.stale_iters = 0 if self.iter_novel else self.stale_iters
        else:
            self.stale_iters += 1
        self.iter_novel = False
        self._calls_this_iter = 0
        x = self._detect_extra()
        if x:
            action, kind, what = x
            self.detections.append(kind)
            if action == "warn":
                return ("warn", kind, loop_message(kind, what))
            return ("stop", kind, f"loop detected: {what}")
        d = self._detect(i)
        if not d:
            return None
        kind, subject, what = d
        self.detections.append(kind)
        if not self.warned:
            self.warned = True
            self.warn_novel_writes = self.novel_writes
            # re-arm: the SAME evidence must repeat once more (not a full threshold again)
            self._rearm(kind, subject)
            return ("warn", kind, loop_message(kind, what))
        if kind in ("repeat_read", "no_progress", "verify_spin") and self.novel_writes > (self.warn_novel_writes or 0):
            # it ignored the warning but produced genuinely new bytes: that is progress, not a loop
            return None
        return ("stop", kind, f"loop detected again after a warning: {what}")

    def _rearm(self, kind, subject):
        c = self.cfg
        if kind == "repeat_read":
            self.read_counts[subject] = c["repeat_read"] - 1
        elif kind == "repeat_write":
            for k in list(self.write_counts):
                if k[0] == subject:
                    self.write_counts[k] = c["repeat_write"] - 1
        elif kind == "write_thrash":
            self.path_returns[subject] = c["aba_returns"] - 1
        elif kind == "verify_spin":
            self.verify_spin_n = c["verify_spin"] - 2
        elif kind == "no_progress":
            self.stale_iters = c["no_progress_iters"] - 2


def loop_message(kind, what):
    todo = {
        "repeat_read": "You already have this information. Stop re-reading; act on it now -- make "
                       "the edit it implies, or run a different command that tests a new hypothesis.",
        "repeat_write": "Re-writing the same bytes changes nothing. Run the verify to see the "
                        "actual failure, then change something that addresses it.",
        "write_thrash": "You are undoing and redoing the same change. Pick one version, then run the "
                        "verify and read its output before touching the file again.",
        "verify_spin": "Re-running it cannot change the result. Read its output, make an edit that "
                       "addresses the first failing check, and only then run it again.",
        "no_progress": "Nothing has changed for several turns. Either make a concrete edit toward "
                       "the failing check, or, if everything is already done, call task_complete.",
        "error_streak": "Repeating the exact same call again will not work. Read the error message and "
                        "either correct the arguments or try a different approach.",
        "alternation": "Going back and forth between the same two calls changes nothing. Pick the "
                       "next DIFFERENT step: make the edit, or run something new.",
        "nav_streak": "You have enough context. Stop exploring: make the edit the task asks for now, "
                      "or run the verify to see the actual failure.",
        "monologue": "Respond with a tool call.",
    }[kind]
    return (f"[loop detected] {what}. {todo} If this repeats once more the run will be stopped.")


# ---------------------------------------------------------------------------------------
# 3b. post-edit syntax guard (SWE-agent edit-linter / Agentless lint-delta, 2026-10-09)
# ---------------------------------------------------------------------------------------
SYNTAX_GUARD_TIMEOUT_S = 10
SYNTAX_GUARD_MAX_BYTES = 2_000_000
_NODE_TS_CHECK = (
    "const m=require('module'),fs=require('fs');"
    "try{m.stripTypeScriptTypes(fs.readFileSync(process.argv[1],'utf8'),{mode:'strip'});process.exit(0)}"
    "catch(e){if(e&&e.code==='ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX'){process.exit(3)}"
    "console.error(String(e&&e.message||e).split('\\n')[0]);process.exit(1)}")
_ESM_HINT_RE = re.compile(r"^\s*(?:import\s|export\s)", re.M)


def syntax_guard_enabled(profile_robust=None, env=None):
    """Kill switches: env WORKER_SYNTAX_GUARD=0, or profile `robust.syntax_guard: false`."""
    env = os.environ if env is None else env
    ev = str(env.get("WORKER_SYNTAX_GUARD", "")).strip().lower()
    if ev in ("0", "false", "off", "no"):
        return False
    if (profile_robust or {}).get("syntax_guard") is False:
        return False
    return True


def _node_check(argv_tail, timeout):
    import subprocess
    import shutil
    node = shutil.which("node")
    if not node:
        return None
    try:
        r = subprocess.run([node, "--disable-warning=ExperimentalWarning"] + argv_tail,
                           capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    if r.returncode == 0 or r.returncode == 3:
        return None
    msg = (r.stderr or "").strip().splitlines()
    # node --check prints "file:LINE\n<src>\n   ^\n\nSyntaxError: msg"
    for ln in msg:
        if "Error" in ln:
            return ln.strip()[:300]
    return (msg[0] if msg else "syntax error")[:300]


def syntax_error_for(path, text, timeout=SYNTAX_GUARD_TIMEOUT_S):
    """-> the parser error string, or None when the text parses OR the type is unknown/unchecked.
    Never raises. python: ast.parse; json: json.loads; yaml: safe_load_all; .js/.mjs/.cjs: node
    --check; .ts/.mts/.cts: node's stripTypeScriptTypes (offline parser, erasable syntax only --
    enums/namespaces are skipped, not flagged). .tsx/.jsx are skipped (no JSX-safe fast parser)."""
    try:
        if text is None or len(text) > SYNTAX_GUARD_MAX_BYTES:
            return None
        ext = os.path.splitext(str(path))[1].lower()
        if ext in (".py", ".pyi"):
            import ast
            try:
                ast.parse(text)
            except SyntaxError as e:
                return f"SyntaxError: {e.msg} (line {e.lineno})"
            except ValueError as e:
                return f"ValueError: {e}"
            return None
        if ext == ".json":
            try:
                json.loads(text)
            except ValueError as e:
                return f"JSONDecodeError: {e}"
            return None
        if ext in (".yaml", ".yml"):
            try:
                import yaml
            except Exception:
                return None
            try:
                list(yaml.safe_load_all(text))
            except yaml.YAMLError as e:
                return "YAMLError: " + " ".join(str(e).split())[:250]
            except Exception:
                return None
            return None
        if ext in (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts"):
            import tempfile
            with tempfile.TemporaryDirectory(prefix="wsyn-") as td:
                is_ts = ext in (".ts", ".mts", ".cts")
                # check a COPY: the text under test, never the live worktree file
                if is_ts:
                    f = os.path.join(td, "x" + ext)
                    open(f, "w").write(text)
                    return _node_check(["-e", _NODE_TS_CHECK, f], timeout)
                esm = ext == ".mjs" or (ext == ".js" and _ESM_HINT_RE.search(text))
                f = os.path.join(td, "x.mjs" if esm else ("x.cjs" if ext == ".cjs" else "x.js"))
                open(f, "w").write(text)
                return _node_check(["--check", f], timeout)
    except Exception:
        return None
    return None


def syntax_delta(path, before, after, timeout=SYNTAX_GUARD_TIMEOUT_S):
    """-> an error string ONLY when `after` has a syntax error and `before` did not (a file that
    was already broken never blocks an edit -- a parser reports just the first error, so fixing it
    legitimately reveals the next). Never raises."""
    try:
        err = syntax_error_for(path, after, timeout)
        if not err:
            return None
        if before is not None and syntax_error_for(path, before, timeout):
            return None
        return err
    except Exception:
        return None


# ---------------------------------------------------------------------------------------
# 4. stop-gate
# ---------------------------------------------------------------------------------------
def check_stop_criteria(cwd, required_files=(), must_contain=(), verify_ok=None, verify_out="",
                        extra_files=()):
    """Re-checks ALL criteria (never only the previously failing ones).
    -> (ok, failures) with failures a list of human-readable strings.
      * verify_ok: True/False from the worker's _quick_verify, or None when there is no verify
      * required_files: paths (relative to cwd) that must exist and be non-empty
      * must_contain: [(path_or_None, literal)] -- literal must appear in that file (or, with
        path None, in ANY required file or any of `extra_files`, e.g. the files changed this run)."""
    from pathlib import Path
    cwd = Path(cwd)
    failures = []
    if verify_ok is False:
        tail = (verify_out or "").strip()[-600:]
        failures.append("verify command exits non-zero" + (f":\n{tail}" if tail else ""))
    texts = {}
    for f in required_files:
        p = cwd / f
        try:
            ok = p.is_file() and p.stat().st_size > 0
        except OSError:
            ok = False
        if not ok:
            failures.append(f"required file missing or empty: {f}")
        else:
            try:
                texts[f] = p.read_text(errors="replace")
            except OSError:
                texts[f] = ""
    for item in must_contain:
        path, lit = item if isinstance(item, (tuple, list)) else (None, item)
        if path:
            if path not in texts:
                try:
                    texts[path] = (cwd / path).read_text(errors="replace")
                except OSError:
                    texts[path] = None
            body = texts[path]
            if body is None:
                failures.append(f"must-contain literal {lit!r}: file {path} is missing")
            elif lit not in body:
                failures.append(f"must-contain literal not found in {path}: {lit!r}")
        else:
            for ef in extra_files:
                if ef not in texts:
                    try:
                        texts[ef] = (cwd / ef).read_text(errors="replace")
                    except (OSError, UnicodeError):
                        texts[ef] = ""
            if not any(lit in t for t in texts.values() if t):
                failures.append(f"must-contain literal not found in the target or any changed file: {lit!r}")
    return (not failures), failures


def stop_gate_message(failures, attempt, limit):
    lines = "\n".join(f"  - {f}" for f in failures)
    return (f"NOT ACCEPTED (stop-gate {attempt}/{limit}): task_complete is only accepted when ALL "
            f"completion criteria hold. Failing right now:\n{lines}\n"
            f"Fix these, then call task_complete again. Every criterion is re-checked each time.")


_PATH_RE = re.compile(r"^[\w@./\-]+\.[A-Za-z0-9]{1,8}$")


def parse_entry_point(task_text):
    """The `## Entry point` path (`lib/x.ts:12` -> `lib/x.ts`), or None when it is not a file path."""
    lines = (task_text or "").splitlines()
    for idx, line in enumerate(lines):
        if re.match(r"^#{1,6}\s*entry point\s*$", line.strip(), re.I):
            for nxt in lines[idx + 1:]:
                t = nxt.strip().strip("`")
                if not t:
                    continue
                t = re.sub(r":\d+(?::\d+)?$", "", t.split()[0] if t.split() else "")
                return t if _PATH_RE.match(t) and not t.startswith(("/", "..")) else None
    return None


def parse_task_criteria(task_text):
    """(required_files, must_contain) from a TASK.md-style spec, conservative:
      * required_files: the `## Entry point` file, plus backticked paths under a
        `## Required files` / `## Files to create` heading
      * must_contain: bullets under `## Must contain`; a bare bullet -> (None, literal) meaning
        "the default target (entry point) or any file changed this run"; `in <path>: `tok``
        pins it to <path> -> (path, literal). Literals containing TODO are ignored.
    Absent sections yield nothing -- the gate only enforces what the spec actually states."""
    files, lits = [], []
    ep = parse_entry_point(task_text)
    if ep:
        files.append(ep)
    sect = None
    for line in (task_text or "").splitlines():
        s = line.strip()
        hm = re.match(r"^#{1,6}\s*(.+?)\s*:?\s*$", s)
        if hm:
            t = hm.group(1).lower()
            if re.fullmatch(r"must[- ]contain", t):
                sect = "lit"
            elif re.fullmatch(r"required files?|files to (?:create|write|add)", t):
                sect = "files"
            else:
                sect = None
            continue
        if sect and re.match(r"^[-*]\s+", s):
            toks = re.findall(r"`([^`]+)`", s)
            if sect == "lit":
                pin = re.match(r"^[-*]\s+in\s+([\w@./\-]+)\s*:", s)
                for tk in toks:
                    if "TODO" in tk:
                        continue
                    if pin and tk == pin.group(1):
                        continue
                    lits.append((pin.group(1) if pin else None, tk))
            else:
                files.extend(t for t in toks if _PATH_RE.match(t) and not t.startswith(("/", "..")))
    seen, out = set(), []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out, lits


# ---------------------------------------------------------------------------------------
# 5. runaway-reasoning budget
# ---------------------------------------------------------------------------------------
class ReasoningBudget:
    """Streamed-turn guard. Feed reasoning/content chunk sizes; `exceeded()` is True once the turn
    has spent `budget_chars` of reasoning with no content and no tool call. Default 24000 chars
    (~6k tokens) -- real converging turns in the transcripts think for <4k chars at the median
    (see calibration in the report)."""

    def __init__(self, budget_chars=24000):
        self.budget = int(budget_chars)
        self.reasoning = 0
        self.content = 0

    def add_reasoning(self, n):
        self.reasoning += n

    def add_content(self, n):
        self.content += n

    def exceeded(self):
        return self.content == 0 and self.reasoning >= self.budget


def reasoning_runaway_message(chars):
    return ("[reasoning budget] your previous turn spent ~%d characters thinking without making a "
            "tool call, so it was cut off. Do NOT think this through again at length. Decide in "
            "one or two sentences and make exactly one tool call now." % chars)


# ---------------------------------------------------------------------------------------
# 6. bash-only action mode
# ---------------------------------------------------------------------------------------
_BASH_BLOCK_RE = re.compile(r"```(?:bash|sh|shell)[ \t]*\n(.*?)```", re.DOTALL)


def extract_bash_block(content):
    """mini-swe-agent: exactly ONE ```bash block per turn. -> ("ok", cmd) | ("none", None) |
    ("multiple", n). Think-tag leakage is stripped first. `echo TASK_COMPLETE` (alone) = done."""
    visible = strip_think(content)[0]
    blocks = _BASH_BLOCK_RE.findall(visible)
    if not blocks:
        return "none", None
    if len(blocks) > 1:
        return "multiple", len(blocks)
    cmd = blocks[0].strip()
    return ("ok", cmd) if cmd else ("none", None)


def bash_mode_is_done(cmd):
    return cmd.strip().splitlines()[:1] == ["echo TASK_COMPLETE"] and len(cmd.strip().splitlines()) == 1


BASH_MODE_PROMPT = (
    "Respond with EXACTLY ONE fenced ```bash block per turn containing the single shell command to "
    "run, preceded by at most two sentences of reasoning. Write files with heredocs (cat > f <<'EOF'). "
    "When the task is complete, reply with a block containing only: echo TASK_COMPLETE")


def bash_mode_error(kind, n=None):
    if kind == "multiple":
        return (f"Format error: you sent {n} bash blocks. Send EXACTLY ONE ```bash block per turn.")
    return "Format error: no ```bash block found. Send EXACTLY ONE ```bash block with the next command."


# ---------------------------------------------------------------------------------------
# Observation masking (2026-10-09). Old bulky TOOL OUTPUTS are replaced, in the copy of the
# transcript SENT to the model, by a short deterministic placeholder (tool call kept, one-line
# summary, how to get it back). The model's reasoning and actions stay in full, the saved
# transcript is never touched, and the most recent verify output is never masked. No LLM
# summariser. Prefix stability: masking only advances in strides (`obs_mask_stride` turns), so
# between advances the sent prefix is byte-identical turn to turn (prompt-cache friendly).
OBS_MASK_DEFAULTS = {"keep_turns": 8, "min_chars": 2000, "stride": 4}
_TOOL_USER_RE = re.compile(r"^\[tool result for ([A-Za-z0-9_.:-]+)\]: ", re.S)
_VERIFY_CMD_RE = re.compile(
    r"pytest|unittest|node\s+--test|npm\s+(run\s+)?test|\btest[-_][\w.-]+\.(py|sh|js)|auto-harness-check"
    r"|make\s+test|cargo\s+test|go\s+test|\bjest\b|\bvitest\b|\btsc\b", re.I)


def obs_mask_enabled(profile_robust=None, env=None):
    """Kill switches: env WORKER_OBS_MASK=0, or profile `robust.obs_mask: false`."""
    env = os.environ if env is None else env
    if str(env.get("WORKER_OBS_MASK", "")).strip().lower() in ("0", "false", "off", "no"):
        return False
    return (profile_robust or {}).get("obs_mask") is not False


def obs_mask_config(profile_robust=None):
    rb = profile_robust or {}
    cfg = dict(OBS_MASK_DEFAULTS)
    for k, rk in (("keep_turns", "obs_mask_keep_turns"), ("min_chars", "obs_mask_min_chars"),
                  ("stride", "obs_mask_stride")):
        try:
            if rb.get(rk) is not None:
                cfg[k] = max(0, int(rb[rk]))
        except (TypeError, ValueError):
            pass
    cfg["stride"] = max(1, cfg["stride"])
    return cfg


def _call_args(tc):
    fn = (tc or {}).get("function") or {}
    raw = fn.get("arguments", {})
    if isinstance(raw, dict):
        return fn.get("name") or "", raw
    try:
        v = json.loads(raw or "{}")
        return fn.get("name") or "", v if isinstance(v, dict) else {}
    except Exception:
        return fn.get("name") or "", {}


def _tool_result_slots(messages):
    """-> {msg_index: (name, args, text)} for every tool output message. Native/openai tool
    messages are matched to their call by tool_call_id, else positionally within the preceding
    assistant turn; manual-tools results ("[tool result for NAME]: ...") carry the name."""
    out = {}
    calls, pos = [], 0
    for idx, m in enumerate(messages):
        role = m.get("role")
        if role == "assistant":
            calls = [_call_args(tc) + (tc.get("id"),) for tc in (m.get("tool_calls") or [])]
            pos = 0
            continue
        text = m.get("content")
        if not isinstance(text, str):
            continue
        if role == "tool":
            hit = None
            tcid = m.get("tool_call_id")
            if tcid:
                hit = next((c for c in calls if c[2] == tcid), None)
            elif pos < len(calls):
                hit = calls[pos]
            pos += 1
            name, args = (hit[0], hit[1]) if hit else ("tool", {})
            out[idx] = (name, args, text)
        elif role == "user":
            mm = _TOOL_USER_RE.match(text)
            if mm:
                out[idx] = (mm.group(1), {}, text[mm.end():])
    return out


def _is_verify_output(name, args, text, verify):
    if name != "run_bash":
        return False
    cmd = str(args.get("command") or "")
    if verify and verify.strip() and verify.strip() in cmd:
        return True
    if cmd and _VERIFY_CMD_RE.search(cmd):
        return True
    return False


def _first_line(text, n=100):
    for ln in str(text).splitlines():
        ln = ln.strip()
        if ln and ln not in ("{", "}"):
            return ln[:n]
    return ""


def obs_placeholder(name, args, text):
    """Deterministic one-line stand-in for a bulky tool output. Pure."""
    n = len(text)
    lines = text.count("\n") + 1
    if name == "read_file":
        path = args.get("path", "?")
        off = args.get("offset")
        again = f"read_file path={path}" + (f" offset={off}" if off is not None else "")
        what = f"read_file {path}" + (f" (offset {off})" if off is not None else "")
        return (f"[old output elided to save context: {what}, {n} chars / {lines} lines; first line: "
                f"{_first_line(text)!r}. Re-read with {again} if you need it again.]")
    if name == "run_bash":
        cmd = str(args.get("command") or "?").replace("\n", " ")[:160]
        rc = re.search(r'"exit_code":\s*(-?\d+)', text)
        rcs = f" exit_code={rc.group(1)}" if rc else ""
        return (f"[old output elided to save context: run_bash `{cmd}`{rcs}, {n} chars. "
                f"Re-run the command if you need the output again.]")
    desc = ", ".join(f"{k}={str(v)[:60]!r}" for k, v in list(args.items())[:3])
    return (f"[old output elided to save context: {name}({desc}), {n} chars / {lines} lines; first line: "
            f"{_first_line(text)!r}. Call the tool again if you need it.]")


def mask_observations(messages, cfg=None, state=None, verify=None):
    """-> (view, stats). `view` is `messages` with old bulky tool outputs replaced by placeholders
    (shallow copies only; `messages` is never mutated). An output is masked when it is older
    than the last `keep_turns` assistant turns (advanced in `stride` steps via `state`, a dict the
    caller keeps for the run), at least `min_chars` long, and not the most recent verify output.
    ERROR results are kept (they are short and instructive)."""
    cfg = cfg or dict(OBS_MASK_DEFAULTS)
    state = state if state is not None else {}
    keep, min_chars, stride = cfg["keep_turns"], cfg["min_chars"], cfg["stride"]
    asst = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
    keep = max(1, keep)
    cand = asst[-keep] if len(asst) >= keep else 0
    rank = sum(1 for a in asst if a < cand)          # assistant turns before the candidate boundary
    if rank - state.get("rank", 0) >= stride:
        state["rank"], state["boundary"] = rank, cand
    boundary = state.get("boundary", 0)
    stats = {"masked": 0, "chars_saved": 0, "boundary": boundary}
    if boundary <= 0:
        return messages, stats
    slots = _tool_result_slots(messages)
    last_verify = None
    for idx in sorted(slots):
        name, args, text = slots[idx]
        if _is_verify_output(name, args, text, verify):
            last_verify = idx
    view = list(messages)
    for idx, (name, args, text) in slots.items():
        if idx >= boundary or idx == last_verify or len(text) < min_chars:
            continue
        if text.lstrip().startswith("ERROR"):
            continue
        ph = obs_placeholder(name, args, text)
        if len(ph) >= len(text):
            continue
        m = dict(messages[idx])
        m["content"] = (messages[idx]["content"][:_TOOL_USER_RE.match(messages[idx]["content"]).end()] + ph
                        if messages[idx].get("role") == "user" else ph)
        view[idx] = m
        stats["masked"] += 1
        stats["chars_saved"] += len(text) - len(ph)
    return view, stats


class SamplingLadder:
    """PURE state machine for the sampling escalation ladder (default OFF; `enabled` False => every
    method is inert and the worker's request bodies are untouched).

    on_abort(kind): called when a turn is aborted/cut (output cap, prose runaway, reasoning runaway).
      abort 1                       -> ("step", 1)   next turn only: temp>=0.8, rep 1.2, fresh seed
      2nd abort on the SAME failing check -> ("exit", REASON_SPEC_DEFECT_REPEAT)  spec-defect path FIRST
      abort 2, different/unknown    -> ("step", 2)   next turn only: presence ~1.0, thinking off (hybrid arm)
      abort 3+                      -> ("exit", REASON_LADDER_EXHAUSTED)    scheduler re-specs
    A turn that produced a tool call resets the consecutive count (`on_ok`). `take()` returns
    (step, seed) for the NEXT turn exactly once, with a fresh seed per call (a new seed per
    retry/refine round: `seed_base` is round-specific and the counter advances every take)."""

    def __init__(self, enabled=False, seed_base=0):
        self.enabled = bool(enabled)
        self.seed_base = int(seed_base or 0)
        self.aborts = 0
        self.pending = 0
        self.by_check = {}         # failing-check signature -> aborts seen while it was the failing check
        self.last_check = None     # latest known failing-check signature (fed by note_check)
        self.takes = 0
        self.history = []

    def note_check(self, sig):
        s = frozenset(sig or ())
        self.last_check = s or None

    def on_abort(self, kind="cut"):
        if not self.enabled:
            return ("off", None)
        self.aborts += 1
        # per-check count survives a good turn in between: two aborts while the SAME check is the
        # failing one is a spec problem however the turns were interleaved.
        same = False
        if self.last_check is not None:
            self.by_check[self.last_check] = self.by_check.get(self.last_check, 0) + 1
            same = self.by_check[self.last_check] >= 2
        self.history.append((self.aborts, kind, same))
        if same:
            self.pending = 0
            return ("exit", REASON_SPEC_DEFECT_REPEAT)
        if self.aborts >= 3:
            self.pending = 0
            return ("exit", REASON_LADDER_EXHAUSTED)
        self.pending = self.aborts
        return ("step", self.aborts)

    def on_ok(self):
        if self.enabled:
            self.aborts = 0

    def take(self):
        """(step, seed) for the next turn; (0, None) when nothing is pending. One-shot."""
        if not self.enabled or not self.pending:
            return (0, None)
        step, self.pending = self.pending, 0
        self.takes += 1
        return (step, (self.seed_base * 1009 + self.takes * 7919 + 17) % (2 ** 31 - 1))


def loop_exhaust_exit_enabled(profile_robust=None, env=None):
    """robust.loop_exhaust_exit (default OFF) / env WORKER_LOOP_EXHAUST_EXIT=1: when the reasoning-loop
    recovery ladder is spent, end the run with the named reason REASON_LOOP_EXHAUSTED (scheduler
    re-specs) instead of parking it paused for a resume that re-enters the same loop."""
    env = os.environ if env is None else env
    v = env.get("WORKER_LOOP_EXHAUST_EXIT")
    if v is not None and v != "":
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool((profile_robust or {}).get("loop_exhaust_exit"))
