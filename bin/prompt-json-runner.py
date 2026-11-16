#!/usr/bin/env python3
"""prompt-json-runner.py -- ollama-queue --runner for prompt jobs (prompt_jobs.py, POST /api/prompt-jobs), 2026-11-16.

One app request = one queue row = this runner: a system prompt + a text go to the lane's model, and the model's JSON
object comes back in result.json. First user: resell-tracker's backup parser for order emails/pages it could not parse.

Queue contract (same five flags as the other runners): --model --host --num-ctx --cwd --task-file.
  --host   the lane the queue picked: the Darkbloom endpoint (OpenAI protocol, via darkbloom_chat) or an Ollama URL
  --model  the model the queue picked for that lane (Darkbloom: the model it has loaded; Ollama: PROMPT_JOB_MODEL)
  --cwd    the job's private dir (0700); --task-file is <cwd>/task.json: {"system", "text", "json", "max_tokens"}
Writes <cwd>/result.json (0600, atomic): {"ok", "result", "error", "model", "lane", "seconds", "attempts"}.
error codes: E_TASK (task unreadable), E_LANE (the model could not be reached twice), E_NOT_JSON (no JSON object
after one retry), E_EMPTY (empty answer).
Always exits 0 once result.json is written, so a failed parse is an ordinary finished row: nothing retries it,
nothing escalates it, and it never spends a paid model. Prints only ids, sizes, timing and codes, never the text.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

JSON_HINT = "\n\nAnswer with one JSON object only, no other text."
RETRY_HINT = "\n\n(Your last answer was not a valid JSON object. Reply with the JSON object only.)"
MAX_TOKENS_CAP = 4096


def emit(msg):
    print("[prompt-json-runner] " + msg, flush=True)


def resolve_host(host):
    """The queue passes a URL; an alias ("unraid") is mapped through the worker's host table like the other runners."""
    h = (host or "").strip().rstrip("/")
    if "://" in h:
        return h
    try:
        import importlib.util as ilu
        spec = ilu.spec_from_file_location("ollama_worker_pj", os.path.join(HERE, "ollama-worker.py"))
        w = ilu.module_from_spec(spec)
        spec.loader.exec_module(w)
        url = w.host_url(h)
        if url:
            return url.rstrip("/")
    except Exception:
        pass
    return None


def is_darkbloom(host):
    try:
        import darkbloom_chat as dbk
        return dbk.is_darkbloom(host)
    except Exception:
        return False


def ollama_chat(base, model, num_ctx, system, user, want_json, max_tokens, timeout=600):
    body = {"model": model, "stream": False, "keep_alive": "5m", "think": False,
            "options": {"temperature": 0, "num_ctx": num_ctx, "num_predict": max_tokens},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if want_json:
        body["format"] = "json"
    for attempt in (1, 2):
        req = urllib.request.Request(base + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            return (d.get("message") or {}).get("content") or ""
        except urllib.error.HTTPError as e:
            # a model without thinking support refuses the field: once more without it
            if attempt == 1 and e.code == 400 and "think" in body:
                body.pop("think")
                continue
            raise


def darkbloom_chat(host, model, system, user, want_json, max_tokens):
    import darkbloom_chat as dbk
    content, _finish = dbk.chat(host, model, system, user, schema=({"type": "object"} if want_json else None),
                                temperature=0, max_tokens=max_tokens, role="review")
    return content or ""


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def parse_object(raw):
    """The model's answer as a JSON object (dict), or None. Takes a ```json fence or the outermost {...}."""
    s = (raw or "").strip()
    s = re.sub(r"(?s)<think>.*?</think>", "", s).strip()
    m = _FENCE.match(s)
    if m:
        s = m.group(1)
    for cand in (s, s[s.find("{"):s.rfind("}") + 1] if "{" in s and "}" in s else ""):
        if not cand:
            continue
        try:
            d = json.loads(cand)
        except ValueError:
            continue
        if isinstance(d, dict):
            return d
    return None


def write_result(cwd, rec):
    p = Path(cwd) / "result.json"
    tmp = p.with_name(".result.json.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, p)


def run(a, call=None):
    """-> the result record (also written). `call(system, user)` is injectable for tests."""
    t0 = time.time()
    jid = Path(a.cwd).name
    rec = {"ok": False, "result": None, "error": None, "model": a.model, "lane": None, "seconds": 0, "attempts": 0}
    try:
        task = json.loads(Path(a.task_file).read_text())
        system, text = str(task.get("system") or ""), str(task.get("text") or "")
        want_json = task.get("json", True) is not False
        max_tokens = max(64, min(int(task.get("max_tokens") or 2048), MAX_TOKENS_CAP))
        if not text:
            raise ValueError("no text")
    except (OSError, ValueError, TypeError, AttributeError):
        rec["error"] = "E_TASK"
        write_result(a.cwd, rec)
        emit("%s: E_TASK" % jid)
        return rec
    if call is None:
        base = resolve_host(a.host)
        db = bool(base) and is_darkbloom(base)
        rec["lane"] = "darkbloom" if db else "ollama"
        if not base:
            rec["error"] = "E_LANE"
            write_result(a.cwd, rec)
            emit("%s: E_LANE (unknown host)" % jid)
            return rec

        def call(sy, us):
            if db:
                return darkbloom_chat(base, a.model, sy, us, want_json, max_tokens)
            return ollama_chat(base, a.model, a.num_ctx, sy, us, want_json, max_tokens)
    sys_full = system + (JSON_HINT if want_json else "")
    user = text
    raw, lane_fail = None, 0
    for attempt in (1, 2):
        rec["attempts"] = attempt
        try:
            raw = call(sys_full, user)
        except (urllib.error.URLError, OSError, ValueError, RuntimeError) as e:
            lane_fail += 1
            emit("%s: call %d failed (%s)" % (jid, attempt, type(e).__name__))
            if attempt == 1:
                time.sleep(float(os.environ.get("PROMPT_JOB_RETRY_S", "20")))
            continue
        if not want_json:
            break
        obj = parse_object(raw)
        if obj is not None:
            rec["result"] = obj
            break
        user = text + RETRY_HINT
    if lane_fail == 2:
        rec["error"] = "E_LANE"
    elif not want_json:
        if raw and raw.strip():
            rec["result"] = {"text": raw.strip()}
        else:
            rec["error"] = "E_EMPTY"
    elif rec["result"] is None:
        rec["error"] = "E_EMPTY" if not (raw or "").strip() else "E_NOT_JSON"
    rec["ok"] = rec["error"] is None
    rec["seconds"] = round(time.time() - t0, 1)
    write_result(a.cwd, rec)
    emit("%s: %s, %d chars in, %d chars out, %d attempt(s), %.0fs" % (
        jid, "ok" if rec["ok"] else rec["error"], len(system) + len(text), len(raw or ""), rec["attempts"],
        rec["seconds"]))
    return rec


def main(argv=None):
    ap = argparse.ArgumentParser(description="prompt job (queue runner): one prompt, JSON back")
    ap.add_argument("--model", required=True)
    ap.add_argument("--host", required=True)
    ap.add_argument("--num-ctx", type=int, default=12288)
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--task-file", required=True)
    a = ap.parse_args(argv)
    try:
        run(a)
    except OSError as e:          # could not even write result.json: the row fails, the API says E_QUEUE
        emit("cannot write result (%s)" % type(e).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
