"""queue_remote_cli -- `ollama-queue.py` as a thin client when the queue lives on another machine
(queue_config.queue_is_remote()). Each forwarded subcommand calls the matching route of the queue's HTTP API with the bearer
token; nothing touches a local state file. Subcommands with a flag-free route use it; every other allowed subcommand
(plan-add, focus, accept-bundle, escalate, ..., and the flag forms of cancel / stop / hold / resume / promote) and all of
`qctl` run on the queue host through passthrough() = POST /api/cli, which returns the command's rc / stdout / stderr.
escalate --gate-output FILE sends the file's last 4000 chars inline. Subcommands that need the queue host (run, enqueue
of a local cwd, ...) exit with a message naming the right tool. Never prints the token."""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import queue_config

# Subcommands whose local implementation already talks to the queue API (queue_config.queue_api()), so they run as is:
# clear-card posts to it in both modes; results (GET /api/results) and needs-opus (GET /api/state) read it when remote.
API_NATIVE = {"clear-card", "results", "needs-opus"}

# Subcommands (and every qctl verb) the queue API's POST /api/cli runs on the queue host; the server holds the same list.
# The ones with a per-route forward below keep it for their flag-free form.
PASSTHROUGH_VERBS = frozenset((
    "plan-add", "plan-clear", "focus", "accept-bundle", "unpin-group", "bundle", "nsample", "ttc", "escalate",
    "retag-bundles", "hold", "resume", "promote", "cancel", "stop", "resolve", "promote-group", "run-now", "move-group",
    "status"))
CLI_TIMEOUT = 200          # the server gives a command 180 s

HOST_ONLY = {
    "run": "the daemon runs inside the queue container",
    "enqueue": "use mini-submit (it uploads the spec and its inputs); a bare enqueue names a cwd the queue cannot see",
    "enqueue-gpu": "use mini-submit",
}


def _call(method, path, payload=None, timeout=30):
    base, _tok = queue_config.queue_api()
    headers = dict(queue_config.queue_api_headers())
    data = b"" if method == "POST" else None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        sys.exit("[queue] cannot reach the queue API at %s: %s" % (base, type(e).__name__))


def _show(status, body, ok_msg=None):
    if status == 200:
        print(ok_msg or body.strip())
        return
    msg = body.strip()
    try:
        msg = json.loads(body).get("error") or msg
    except (ValueError, AttributeError):
        pass
    sys.exit("[queue] %s (HTTP %d)" % (msg[:300], status))


def _job_for_key(key):
    """The id of a job in the bundle `key` names. `key` is a job id (used as is) or a bundle/group key; the API routes
    take a job id and resolve its bundle, so a bare group key is looked up in /api/jobs (a pending row first). A key
    that matches nothing is passed on unchanged and the server answers with its own error."""
    st, body = _call("GET", "/api/jobs")
    if st != 200:
        return key
    try:
        data = json.loads(body)
        rows = data.get("jobs", data) if isinstance(data, dict) else data
    except ValueError:
        return key
    rows = [j for j in rows if isinstance(j, dict)] if isinstance(rows, list) else []
    if any(j.get("id") == key for j in rows):
        return key
    members = [j for j in rows if key in (j.get("group_key"), j.get("bundle"))]
    first = next((j for j in members if j.get("status") == "pending"), members[0] if members else None)
    return (first.get("id") or key) if first else key


def _flags_used(args, *names):
    return [n for n in names if getattr(args, n, False)]


def _inline_gate_output(argv):
    """escalate --gate-output X: when X names a file on THIS machine, send its last 4000 chars (the bound cmd_escalate
    keeps) instead of the path, which the queue host cannot read. Anything else is left as typed."""
    out, i = list(argv), 0
    while i < len(out):
        a = out[i]
        if a == "--gate-output" and i + 1 < len(out):
            idx, val = i + 1, out[i + 1]
            pre = ""
        elif a.startswith("--gate-output="):
            idx, val, pre = i, a.split("=", 1)[1], "--gate-output="
        else:
            i += 1
            continue
        try:
            if val and os.path.isfile(val):
                with open(val, encoding="utf-8", errors="replace") as f:
                    out[idx] = pre + f.read()[-4000:]
        except OSError:
            pass
        i = idx + 1
    return out


def passthrough(tool, argv):
    """Run `tool argv` on the queue host (POST /api/cli) and exit with its return code, its stdout / stderr written to
    ours. A refusal or a transport failure exits through _show / _call with the server's message."""
    argv = [str(a) for a in argv]
    if tool == "ollama-queue.py" and argv and argv[0] == "escalate":
        argv = _inline_gate_output(argv)
    st, body = _call("POST", "/api/cli", {"tool": tool, "argv": argv}, timeout=CLI_TIMEOUT)
    if st != 200:
        _show(st, body)
    try:
        res = json.loads(body)
        rc = int(res.get("rc", 1))
        out, err = str(res.get("stdout") or ""), str(res.get("stderr") or "")
    except (ValueError, AttributeError, TypeError):
        sys.exit("[queue] unreadable answer from /api/cli")
    if res.get("truncated"):
        err += "[queue] output was cut to its last 256 KB\n"
    sys.stdout.write(out)
    sys.stdout.flush()
    sys.stderr.write(err)
    sys.stderr.flush()
    sys.exit(rc)


def forward(args, raw_argv=None):
    """True when the subcommand was handled here (the caller returns); exits for host-only ones; False for local mode.
    `raw_argv` is the command line as typed (default sys.argv[1:]); the subcommands without a per-route forward, and the
    flag forms the routes cannot carry, run on the queue host through passthrough()."""
    if not queue_config.queue_is_remote():
        return False
    if raw_argv is None:
        raw_argv = sys.argv[1:]
    cmd = args.cmd
    if cmd in API_NATIVE:
        return False
    jid = urllib.parse.quote(str(getattr(args, "job_id", "") or ""), safe="")
    if cmd in HOST_ONLY:
        sys.exit("[queue] `%s` is not available when the queue is remote: %s" % (cmd, HOST_ONLY[cmd]))
    if cmd == "resolve":
        _show(*_call("POST", "/api/jobs/%s/resolve" % jid), ok_msg="[queue] resolved %s" % getattr(args, "job_id", ""))
    elif cmd == "cancel":
        if _flags_used(args, "plan", "automated"):
            passthrough("ollama-queue.py", raw_argv)
        _show(*_call("DELETE", "/api/jobs/%s%s" % (jid, "?force=1" if getattr(args, "force", False) else "")))
    elif cmd == "stop":
        if _flags_used(args, "plan"):
            passthrough("ollama-queue.py", raw_argv)
        _show(*_call("POST", "/api/jobs/%s/kill" % jid))
    elif cmd in ("hold", "resume", "promote"):
        if _flags_used(args, "force", "preempt", "take_focus"):
            passthrough("ollama-queue.py", raw_argv)
        _show(*_call("POST", "/api/jobs/%s/%s" % (jid, cmd)))
    elif cmd == "promote-group":
        _show(*_call("POST", "/api/jobs/%s/promote-group" % urllib.parse.quote(_job_for_key(args.key), safe="")),
              ok_msg="[queue] promoted bundle %s" % args.key)
    elif cmd == "run-now":
        st, body = _call("POST", "/api/jobs/%s/run-now" % urllib.parse.quote(_job_for_key(args.key), safe=""))
        try:
            truth = json.loads(body).get("truth") if st == 200 else None
        except (ValueError, AttributeError):
            truth = None
        _show(st, body, ok_msg=("[queue] %s" % truth) if truth else "[queue] run-now %s" % args.key)
    elif cmd == "move-group":
        payload = {"id": _job_for_key(args.key)}
        if getattr(args, "before", None):
            payload["before_id"] = _job_for_key(args.before)
        _show(*_call("POST", "/api/jobs/move-group", payload), ok_msg="[queue] moved bundle %s" % args.key)
    elif cmd == "status":
        st, body = _call("GET", "/api/jobs")
        if st != 200:
            _show(st, body)
        try:
            data = json.loads(body)
            rows = data.get("jobs", data) if isinstance(data, dict) else data
            for j in rows if isinstance(rows, list) else []:
                print("%-12s %-9s %-24s %s" % (j.get("id", ""), j.get("status", ""), str(j.get("model", ""))[:24],
                                                j.get("label", "")))
        except ValueError:
            print(body.strip())
    elif cmd in PASSTHROUGH_VERBS:
        passthrough("ollama-queue.py", raw_argv)
    else:
        sys.exit("[queue] `%s` is not available when the queue is remote" % cmd)
    return True
