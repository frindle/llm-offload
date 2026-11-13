#!/usr/bin/env python3
"""dispatch-log-hook.py: writes the vault agent-dispatch log line itself, so the main session doesn't spend a model
step on it.

Hook command (both events, async so it never blocks a turn):
  PostToolUse, matcher "Agent":  python3 -I ~/bin/dispatch-log-hook.py
  SubagentStop:                  python3 -I ~/bin/dispatch-log-hook.py

Reads the hook JSON on stdin and writes one line per event to this month's dispatch log through vault-log-append:
  * PostToolUse, foreground agent (the call returned the result): one "ran" line with type/model, description,
    tool uses, tokens, duration and the start of the result.
  * PostToolUse, background agent (status "async_launched"): one "launched" line; the agent is remembered.
  * SubagentStop for a remembered background agent: one "finished" line with elapsed time, tokens (summed from the
    agent's own transcript) and the start of its last message. Stops of foreground agents are skipped (their
    PostToolUse line has everything).
Prints nothing to stdout, so nothing is added to the conversation.

Measured 2026-11-13: in the 7 days before, the main sessions spent 362 Opus calls (~$24 at list prices, ~2.5% of
Opus spend) on nothing but writing these lines after the reminder hook asked for them.

Safety: result text is cut to 240 chars and anything that looks like a credential is masked. If the vault write
fails, the line goes to a local spool file and is sent before the next line. Kill switch: DISPATCH_LOG_HOOK=off.
After a vault write it hands vault-write-tracker.sh a matching event (see tell_tracker).
Env for tests: DISPATCH_LOG_APPEND (append command, text on stdin), DISPATCH_LOG_STATE_DIR (state + spool dir),
DISPATCH_LOG_NOW (epoch seconds), DISPATCH_LOG_TRACKER (tracker path; empty = none)."""
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SNIP = 240
HANDBACK_RE = re.compile(r"report was delivered to you as a message", re.I)
STATE_TTL = 7 * 86400
SECRET_RES = [
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 <masked>"),
    (re.compile(r"(?i)\b([\w-]*(?:token|secret|password|passwd|api[_-]?key|auth)[\w-]*)(\s*[:=]\s*)(['\"]?)[^\s\"',;]{6,}"),
     r"\1\2\3<masked>"),
    (re.compile(r"\b(?:sk|pk|ghp|gho|ghs|github_pat|xox[abp])[-_][A-Za-z0-9_-]{10,}"), "<masked>"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "<masked>"),
    (re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}"), "<masked>"),
]


def now():
    v = os.environ.get("DISPATCH_LOG_NOW")
    return float(v) if v else time.time()


def state_dir():
    d = Path(os.environ.get("DISPATCH_LOG_STATE_DIR") or (Path.home() / ".claude" / "state"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def mask(s):
    for rx, rep in SECRET_RES:
        s = rx.sub(rep, s)
    return s


def snippet(text):
    if isinstance(text, list):
        text = " ".join(c.get("text", "") for c in text if isinstance(c, dict))
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    s = mask(s)
    return (s[:SNIP] + "...") if len(s) > SNIP else s


def one_line(s, n=120):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return mask(s[:n])


def dur(sec):
    sec = int(sec or 0)
    return "%dm%02ds" % (sec // 60, sec % 60) if sec >= 60 else "%ds" % sec


def ktok(n):
    return ("%.1fk" % (n / 1000)) if n else "?"


def stamp():
    return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(now()))


def head(ev):
    proj = os.path.basename((ev.get("cwd") or "").rstrip("/")) or "?"
    sid = (ev.get("session_id") or "")[:8] or "?"
    return "- %s [auto] %s (session %s):" % (stamp(), proj, sid)


class State:
    def __init__(self):
        self.path = state_dir() / "dispatch-log-state.json"
        self.fh = open(state_dir() / "dispatch-log.lock", "a+")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        try:
            self.d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.d = {}
        t = now()
        self.d = {k: v for k, v in self.d.items() if t - v.get("t0", t) < STATE_TTL}

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d))
        os.replace(tmp, self.path)

    def close(self):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def append_cmd():
    c = os.environ.get("DISPATCH_LOG_APPEND") or str(Path.home() / "bin" / "vault-log-append")
    return [c]


def send(text, sid=""):
    """Append text to the dispatch log; spool locally on failure. Returns True when written to the vault."""
    spool = state_dir() / "dispatch-log-spool.md"
    pending = ""
    try:
        pending = spool.read_text()
    except OSError:
        pass
    body = pending + text + "\n"
    try:   # text goes on stdin ("-"), so a line starting with "-" can't be read as an option
        r = subprocess.run(append_cmd() + ["-"], input=body, capture_output=True, text=True, timeout=30)
        ok = r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if ok:
        tell_tracker(sid)
        if pending:
            try:
                spool.unlink()
            except OSError:
                pass
        return True
    with open(spool, "a") as f:
        f.write(text + "\n")
    return False


def tell_tracker(sid):
    """Hand vault-write-tracker.sh the same event a model-written vault-log-append call would produce, so (1) the
    Stop gate in vault-staleness-reminder.sh sees this session's vault write (before, the model's own dispatch-log
    write is what satisfied it) and (2) the drain's ETag map is refreshed, so the line isn't injected back into the
    next prompt as "changed outside this session"."""
    t = os.environ.get("DISPATCH_LOG_TRACKER")
    t = str(Path.home() / ".claude" / "hooks" / "vault-write-tracker.sh") if t is None else t
    if not t or not os.access(t, os.X_OK):
        return
    ev = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": sid,
          "tool_input": {"command": "~/bin/vault-log-append -   # written by dispatch-log-hook.py"}}
    try:
        subprocess.run([t], input=json.dumps(ev), capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass


def transcript_tokens(path):
    """Sum of input+cache+output tokens over the agent's own transcript (one count per API message)."""
    seen, tot = set(), 0
    try:
        with open(path, errors="replace") as fh:
            for raw in fh:
                if '"usage"' not in raw:
                    continue
                try:
                    m = (json.loads(raw).get("message") or {})
                except ValueError:
                    continue
                u, mid = m.get("usage") or {}, m.get("id")
                if not u or mid in seen:
                    continue
                seen.add(mid)
                tot += sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                       "cache_read_input_tokens", "output_tokens"))
    except OSError:
        return 0
    return tot


def on_post_tool_use(ev, st):
    inp = ev.get("tool_input") or {}
    resp = ev.get("tool_response")
    if isinstance(resp, str):
        try:
            resp = json.loads(resp)
        except ValueError:
            resp = {"content": resp}
    resp = resp if isinstance(resp, dict) else {}
    desc = one_line(inp.get("description") or resp.get("description") or "n/a")
    atype = inp.get("subagent_type") or "general-purpose"
    model = resp.get("resolvedModel") or inp.get("model") or "default"
    aid = resp.get("agentId")
    if resp.get("status") == "async_launched":
        if aid:
            st.d[aid] = {"t0": now(), "bg": True, "desc": desc, "type": atype, "model": model, "stops": 0}
        return "%s launched %s/%s in background: \"%s\"." % (head(ev), atype, model, desc)
    if aid:
        st.d[aid] = {"t0": now(), "bg": False, "desc": desc, "type": atype, "model": model, "stops": 0}
    stopped = st.d.pop("stop:" + aid, {}) if aid else {}
    result = snippet(resp.get("content"))
    if HANDBACK_RE.search(result):
        # the report went to the parent as a separate message; use the agent's own last message instead
        result = stopped.get("msg") or "(report sent as a separate message)"
    status = resp.get("status") or "done"
    toks = resp.get("totalTokens") or 0
    stats = "%s, %s tool uses, %s tokens, %s" % (status, resp.get("totalToolUseCount", "?"), ktok(toks),
                                                 dur((resp.get("totalDurationMs") or 0) / 1000))
    return "%s ran %s/%s: \"%s\" (%s). Result: %s" % (head(ev), atype, model, desc, stats,
                                                       result or "(empty)")


def on_subagent_stop(ev, st):
    aid = ev.get("agent_id")
    rec = st.d.get(aid) if aid else None
    if not rec or not rec.get("bg"):
        # a foreground agent: its PostToolUse comes next and writes the line; keep the last message for it
        if aid:
            st.d["stop:" + aid] = {"t0": now(), "msg": snippet(ev.get("last_assistant_message"))}
        return None
    rec["stops"] = rec.get("stops", 0) + 1
    again = " again (resumed)" if rec["stops"] > 1 else ""
    toks = transcript_tokens(ev.get("agent_transcript_path") or "")
    return "%s background %s/%s finished%s: \"%s\" (%s since launch, %s tokens). Last message: %s" % (
        head(ev), rec.get("type"), rec.get("model"), again, rec.get("desc"), dur(now() - rec.get("t0", now())),
        ktok(toks), snippet(ev.get("last_assistant_message")) or "(empty)")


def main():
    if os.environ.get("DISPATCH_LOG_HOOK") == "off":
        return 0
    try:
        ev = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    name = ev.get("hook_event_name")
    st = State()
    try:
        if name == "PostToolUse" and ev.get("tool_name") in ("Agent", "Task"):
            line = on_post_tool_use(ev, st)
        elif name == "SubagentStop":
            line = on_subagent_stop(ev, st)
        else:
            line = None
        st.save()
        if line:
            send(line, re.sub(r"[^A-Za-z0-9._-]", "", ev.get("session_id") or ""))
    finally:
        st.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:   # noqa: BLE001 - a logging hook must never break a turn
        print("dispatch-log-hook: %s" % e, file=sys.stderr)
        sys.exit(0)
