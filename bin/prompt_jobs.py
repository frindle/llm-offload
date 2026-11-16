"""prompt_jobs: prompt-only JSON jobs for apps on the LAN (2026-11-16).

An app posts a system prompt + a text and reads the model's JSON object back later. First user: resell-tracker's
backup parser (order emails / store pages it could not parse; names, street addresses and emails are taken out by the
tracker before it sends; it saves what comes back only as "pending review").

Each job is ONE ordinary queue row (runner prompt-json-runner.py, host_pref "prompt"). The daemon runs it on a FREE
lane only (_prompt_lanes in ollama-queue.py): Darkbloom local with the model it already has loaded (never a switch),
else the Unraid Ollama model (PROMPT_JOB_MODEL), which also waits while Plex plays. No free lane = it waits. Never
paid, never escalated: the runner finishes the row even when the model's answer is not JSON.

  POST   /api/prompt-jobs        {"app", "ref", "system", "text", "json": true, "max_tokens"?}
                                 -> 202 {"id", "status": "pending"}; the same app+ref while live -> 200, same id
  GET    /api/prompt-jobs/<id>   -> {"id", "status": pending|running|done|failed, "lane", "model", "result", "error"}
  DELETE /api/prompt-jobs/<id>   -> {"ok": true} (a pending job is cancelled; a running one -> 409)

Auth: "Authorization: Bearer <QUEUE_PROMPT_TOKEN>" (env, or the file named by QUEUE_PROMPT_TOKEN_FILE); this token
works on these routes only, and the routes accept nothing else. No token configured = 503 (never open).
Privacy: task.json / result.json are 0600 in a 0700 dir <QEL_DIR or ~/.ollama-dispatch>/prompt-jobs/<id>/. Nothing
here logs the text or the result, only ids, sizes and status. Dirs older than KEEP_DAYS are deleted.
"""
import hmac
import json
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROUTE = "/api/prompt-jobs"
RUNNER_NAME = "prompt-json-runner.py"
HOST_PREF = "prompt"
MAX_BODY = 24 * 1024
MAX_SYSTEM = 4 * 1024
MAX_TEXT = 16 * 1024
MAX_LIVE_PER_APP = 20
KEEP_DAYS = 14
MIN_TOKEN_LEN = 24
NUM_CTX = 12288
APP_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
REF_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
ID_RE = re.compile(r"^[0-9a-f]{12}$")
LIVE = ("pending", "queued", "scheduled", "running", "paused", "held")


def model():
    return (os.environ.get("PROMPT_JOB_MODEL") or "qwen3:8b").strip()


def runner_path():
    return str((Path.home() / "bin" / RUNNER_NAME).resolve())


def base_dir():
    return Path(os.environ.get("QEL_DIR") or Path.home() / ".ollama-dispatch").expanduser() / "prompt-jobs"


def token():
    t = (os.environ.get("QUEUE_PROMPT_TOKEN") or "").strip()
    if not t:
        f = (os.environ.get("QUEUE_PROMPT_TOKEN_FILE") or "").strip()
        if f:
            try:
                t = Path(f).expanduser().read_text().strip()
            except OSError:
                t = ""
    return t if len(t) >= MIN_TOKEN_LEN else ""


def auth(headers, tok=None):
    """-> None when allowed, else (status, body)."""
    tok = token() if tok is None else tok
    if not tok:
        return 503, {"error": "E_OFF", "detail": "prompt jobs are off (no QUEUE_PROMPT_TOKEN on the queue)"}
    got = (headers.get("Authorization") or "") if headers is not None else ""
    if got[:7].lower() != "bearer " or not hmac.compare_digest(got[7:].strip().encode(), tok.encode()):
        return 401, {"error": "E_AUTH"}
    return None


def _write_private(path, obj):
    tmp = path.with_name("." + path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _read(path):
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def sweep(base=None, now=None, keep_days=KEEP_DAYS):
    """Delete job dirs older than keep_days (by meta.json created, else dir mtime). -> how many."""
    base = base or base_dir()
    now = time.time() if now is None else now
    n = 0
    try:
        dirs = [d for d in base.iterdir() if d.is_dir() and ID_RE.match(d.name)]
    except OSError:
        return 0
    for d in dirs:
        meta = _read(d / "meta.json") or {}
        try:
            born = float(meta.get("created_ts") or d.stat().st_mtime)
        except OSError:
            continue
        if now - born > keep_days * 86400:
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n


def view(jid, meta, row, res, lane_name=None):
    """PURE. The GET body from the job's meta, its queue row (or None), result.json (or None) and the row's lane
    name as the queue names it ("studio-db" = Darkbloom, "unraid", ...)."""
    out = {"id": jid, "status": "pending", "lane": None, "model": None, "result": None, "error": None}
    if lane_name:
        out["lane"] = "darkbloom" if lane_name in ("studio-db", "darkbloom") else str(lane_name)
    if res is not None:
        out["status"] = "done" if res.get("ok") else "failed"
        out["result"] = res.get("result") if res.get("ok") else None
        out["error"] = None if res.get("ok") else (res.get("error") or "E_FAILED")
        out["model"] = res.get("model")
        if res.get("lane") == "darkbloom":
            out["lane"] = "darkbloom"
        return out
    st = (row or {}).get("status")
    if row is None:
        out.update(status="failed", error="E_LOST")
    elif st == "running":
        out["status"] = "running"
        out["model"] = row.get("model")
    elif st in LIVE:
        out["status"] = "pending"
    elif st in ("done", "done_unconverged"):
        out.update(status="failed", error="E_NO_RESULT")
    else:
        out.update(status="failed", error="E_QUEUE")
    return out


def _body(raw):
    try:
        d = json.loads(raw or b"{}")
    except ValueError:
        return None, (400, {"error": "E_BODY", "detail": "body is not JSON"})
    if not isinstance(d, dict):
        return None, (400, {"error": "E_BODY", "detail": "body must be a JSON object"})
    app, ref = str(d.get("app") or ""), str(d.get("ref") or "")
    system, text = d.get("system") or "", d.get("text")
    if not APP_RE.match(app):
        return None, (400, {"error": "E_BODY", "detail": "app: 1-32 of a-z 0-9 -"})
    if not REF_RE.match(ref):
        return None, (400, {"error": "E_BODY", "detail": "ref: 1-64 of A-Z a-z 0-9 . _ : -"})
    if not isinstance(system, str) or not isinstance(text, str) or not text.strip():
        return None, (400, {"error": "E_BODY", "detail": "system (string) and text (non-empty string) are required"})
    if len(system.encode()) > MAX_SYSTEM or len(text.encode()) > MAX_TEXT:
        return None, (413, {"error": "E_SIZE", "detail": "system <= %d bytes, text <= %d bytes" % (MAX_SYSTEM, MAX_TEXT)})
    want_json = d.get("json", True) is not False
    try:
        mt = int(d.get("max_tokens") or 2048)
    except (TypeError, ValueError):
        mt = 2048
    return {"app": app, "ref": ref, "system": system, "text": text, "json": want_json,
            "max_tokens": max(64, min(mt, 4096))}, None


def _rows_by_id(state):
    return {j.get("id"): j for j in state.get("jobs") or [] if j.get("host_pref") == HOST_PREF}


def create(q, req, base=None, now=None):
    """Make the job dir and append the queue row under the queue's state lock. -> (status, body)."""
    base = base or base_dir()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = time.time() if now is None else now
    with q._Locked() as lock:
        state = lock.load()
        rows = _rows_by_id(state)
        live_app = 0
        for d in base.iterdir():
            if not (d.is_dir() and ID_RE.match(d.name)):
                continue
            meta = _read(d / "meta.json") or {}
            if meta.get("app") != req["app"] or (d / "result.json").exists():
                continue
            row = rows.get(d.name)
            if row is None or row.get("status") not in LIVE:
                continue
            if meta.get("ref") == req["ref"]:
                return 200, {"id": d.name, "status": view(d.name, meta, row, None)["status"]}
            live_app += 1
        if live_app >= MAX_LIVE_PER_APP:
            return 429, {"error": "E_BUSY", "detail": "%d live jobs for %s (max %d)" % (
                live_app, req["app"], MAX_LIVE_PER_APP)}
        ids = {j.get("id") for j in state.get("jobs") or []}
        jid = uuid.uuid4().hex[:12]
        while jid in ids or (base / jid).exists():
            jid = uuid.uuid4().hex[:12]
        d = base / jid
        d.mkdir(mode=0o700)
        _write_private(d / "task.json", {k: req[k] for k in ("system", "text", "json", "max_tokens")})
        _write_private(d / "meta.json", {"id": jid, "app": req["app"], "ref": req["ref"], "created_ts": now,
                                         "in_bytes": len(req["system"].encode()) + len(req["text"].encode())})
        state["jobs"].append({
            "id": jid, "label": "prompt-%s-%s" % (req["app"], jid), "model": model(), "host_pref": HOST_PREF,
            "cwd": str(d), "task_file": str(d / "task.json"), "task_kind": "research", "manual_tools": False,
            "api": "ollama", "verify": None, "runner": runner_path(), "runner_args": None, "num_ctx": NUM_CTX,
            "max_iters": 1, "temperature": 0.0, "chat_timeout": None, "status": "pending",
            "fit_checked": True, "prompt_app": req["app"],
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
            "pid": None, "lane": None, "log_path": None, "exit_code": None,
        })
        lock.save(state)
    return 202, {"id": jid, "status": "pending"}


def _row(q, jid):
    with q._Locked() as lock:
        state = lock.load()
    return next((j for j in state.get("jobs") or [] if j.get("id") == jid), None)


def handle(q, method, path, headers, body=b"", base=None):
    """The whole route: -> (status, obj). `q` is the ollama-queue module (state lock, cancel_job)."""
    denied = auth(headers)
    if denied:
        return denied
    base = base or base_dir()
    p = path.split("?", 1)[0].rstrip("/")
    if p == ROUTE:
        if method != "POST":
            return 405, {"error": "E_METHOD"}
        if len(body or b"") > MAX_BODY:
            return 413, {"error": "E_SIZE", "detail": "body over %d bytes" % MAX_BODY}
        req, err = _body(body)
        if err:
            return err
        sweep(base)
        return create(q, req, base)
    jid = p[len(ROUTE) + 1:] if p.startswith(ROUTE + "/") else ""
    if not ID_RE.match(jid):
        return 404, {"error": "E_NOT_FOUND"}
    d = base / jid
    meta = _read(d / "meta.json")
    if meta is None:
        return 404, {"error": "E_NOT_FOUND"}
    row = _row(q, jid)
    if method == "GET":
        lane = None
        if row is not None and row.get("lane"):
            try:
                lane = q._lane_name(row["lane"])
            except Exception:  # noqa: BLE001
                lane = None
        return 200, view(jid, meta, row, _read(d / "result.json"), lane)
    if method == "DELETE":
        if row is not None and row.get("status") == "running":
            return 409, {"error": "E_RUNNING", "detail": "running; delete it after it finishes"}
        if row is not None and row.get("status") in LIVE:
            try:
                q.cancel_job(jid, explicit=True)
            except Exception:  # noqa: BLE001  (raced to running: keep the dir, say so)
                return 409, {"error": "E_RUNNING", "detail": "it just started; delete it after it finishes"}
        shutil.rmtree(d, ignore_errors=True)
        return 200, {"ok": True}
    return 405, {"error": "E_METHOD"}
