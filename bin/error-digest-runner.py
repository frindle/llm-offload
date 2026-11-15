#!/usr/bin/env python3
"""error-digest-runner.py -- ollama-queue --runner: adds one "what it probably means" line to each NEW error kind in a
day's container error digest (error_digest.py). Free local model on the Unraid lane; one call per container.

Queue contract (same five flags as studio-research.py): --model --host --num-ctx --cwd --task-file.
The task file is {"day_json": "<dir>/digests/<day>.json"}. The model sees only the redacted signatures (times, numbers,
ids, addresses, URLs, emails and secrets already taken out) and the container name. It never decides WHICH errors are
listed (error_digest.py does that from the logs); it only explains, and says "unclear" when it can't tell. Its lines go
into the day JSON under model.lines and the Markdown is re-rendered. Prints only counts, never log text.
Exit 0 when at least one container was answered (or nothing to ask), 1 when every call failed (digest says so).
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
import error_digest as ED  # noqa: E402

MAX_WORDS = 25
SYSTEM = (
    "You explain error lines from home-server docker containers to the owner, who is not a programmer. "
    "For each line, say in at most 20 plain words what it most likely means (what broke or what was refused). "
    "Use only what the line itself and the container name show. Do not guess causes the line does not show, do not "
    "suggest fixes, do not mention numbers or ids (they were removed). If you cannot tell, answer exactly: unclear. "
    "Answer with JSON only.")
SCHEMA = {"type": "object", "properties": {"lines": {"type": "array", "items": {
    "type": "object", "properties": {"id": {"type": "string"}, "meaning": {"type": "string"}},
    "required": ["id", "meaning"]}}}, "required": ["lines"]}


def emit(msg):
    print("[error-digest-runner] " + msg, flush=True)


def resolve_host(host):
    """Queue passes the host as an alias ("unraid"); the worker's host table maps it to a URL."""
    h = (host or "").strip().rstrip("/")
    if "://" in h:
        return h
    try:
        import importlib.util as ilu
        spec = ilu.spec_from_file_location("ollama_worker_ed", os.path.join(HERE, "ollama-worker.py"))
        w = ilu.module_from_spec(spec)
        spec.loader.exec_module(w)
        url = w.host_url(h)
        if url:
            return url.rstrip("/")
    except Exception:
        pass
    raise SystemExit("error-digest-runner: unknown host %r" % host)


def chat(base, model, num_ctx, user, timeout=300):
    body = {"model": model, "stream": False, "keep_alive": "5m", "format": SCHEMA, "think": False,
            "options": {"temperature": 0.1, "num_ctx": num_ctx},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]}
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


def ask_text(container, sigs):
    rows = ["Container: %s" % container, "Lines:"]
    for s in sigs:
        rows.append("- id %s (%s, seen %d times): %s" % (s["id"], s["level"], s["count"], s["text"]))
    rows.append('Answer as {"lines": [{"id": "...", "meaning": "..."}]}, one entry per id.')
    return "\n".join(rows)


def parse(raw, ids):
    """{id: meaning} from the model's JSON: only ids we asked about, one short line each, 'unclear' dropped."""
    try:
        d = json.loads(raw)
    except ValueError:
        m = re.search(r"\{.*\}", raw or "", re.S)
        if not m:
            return {}
        try:
            d = json.loads(m.group(0))
        except ValueError:
            return {}
    out = {}
    for it in (d.get("lines") if isinstance(d, dict) else None) or []:
        if not isinstance(it, dict):
            continue
        i, t = str(it.get("id") or "").strip(), " ".join(str(it.get("meaning") or "").split())
        if i not in ids or not t or t.lower().strip(" .") in ("unclear", "unknown", "n/a"):
            continue
        words = t.split()
        if len(words) > MAX_WORDS:
            t = " ".join(words[:MAX_WORDS]) + "…"
        out[i] = t.replace("`", "'")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="error digest model step (queue runner)")
    ap.add_argument("--model", required=True)
    ap.add_argument("--host", default="unraid")
    ap.add_argument("--num-ctx", type=int, default=8192)
    ap.add_argument("--cwd", default=None)
    ap.add_argument("--task-file", required=True)
    a = ap.parse_args(argv)
    try:
        task = json.loads(Path(a.task_file).read_text())
        day_json = Path(task["day_json"])
        rec = json.loads(day_json.read_text())
    except (OSError, ValueError, KeyError, TypeError):
        emit("E_TASK: task or day file unreadable")
        return 2
    ddir = day_json.parent.parent
    base = resolve_host(a.host)
    items = ED.model_items(rec)
    lines, asked, answered, failed = {}, 0, 0, 0
    t0 = time.time()
    for c, sigs in items.items():
        ids = {s["id"] for s in sigs}
        asked += len(ids)
        try:
            got = parse(chat(base, a.model, a.num_ctx, ask_text(c, sigs)), ids)
        except (urllib.error.URLError, OSError, ValueError) as e:
            failed += 1
            emit("container %d/%d: call failed (%s)" % (list(items).index(c) + 1, len(items), type(e).__name__))
            continue
        answered += len(got)
        lines.update(got)
    # re-read: the digest may have been rebuilt while the calls ran; keep only lines whose kinds are still there
    try:
        rec = json.loads(day_json.read_text())
    except (OSError, ValueError):
        emit("E_TASK: day file vanished")
        return 2
    live = {s["id"] for i in (rec.get("containers") or {}).values() for s in i.get("sigs") or []}
    lines = {k: v for k, v in lines.items() if k in live}
    ok = bool(lines) or not items or failed < len(items)
    rec["model"] = {"status": "done" if ok else "failed", "model": a.model, "asked": asked, "answered": len(lines),
                    "calls_failed": failed, "seconds": round(time.time() - t0, 1), "lines": lines}
    ED.write_day(ddir, rec)
    emit("%s: %d containers, %d kinds asked, %d explained, %d calls failed, %.0fs" % (
        rec.get("day"), len(items), asked, len(lines), failed, time.time() - t0))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
