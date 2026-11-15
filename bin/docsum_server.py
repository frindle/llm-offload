#!/usr/bin/env python3
"""docsum_server.py -- web UI for the private doc-summary feature (stdlib http.server).

    python3 docsum_server.py [--port 7690] [--host 0.0.0.0]

PRIVACY: documents stay on this Mac. There is no pairing, token or cookie: the trust boundary is the LAN or Cloudflare
Access in front of the queue-API proxy (/docsum/). POSTs must be same-origin (Origin matches Host). The log carries ONLY a timestamp,
a route class, an opaque job id, the status code and byte counts -- never a file name, scenario text, query string or
content. log_message is a no-op and error handling prints the exception class name only. The original file name lives in
the private job dir (meta.json) and reaches only the browser of the person who uploaded it.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import docsum_cli  # noqa: E402
import docsum_ui_guard  # noqa: E402
from docsum_private import is_private_label  # noqa: E402
from docsum_ui_jobs import list_jobs  # noqa: E402
import docsum_ui_name  # noqa: E402
from docsum_ui_multipart import parse_multipart  # noqa: E402
from docsum_ui_page import page_html  # noqa: E402

DEFAULT_PORT = 7690
PRIVATE_ROOT = os.path.expanduser(os.environ.get("DOCSUM_PRIVATE_ROOT") or "~/Documents/doc-summaries/private")
STATE = os.path.expanduser("~/bin/ollama-queue-state.json")
QUEUE = os.path.expanduser("~/bin/ollama-queue.py")
MAX_PDF = 100 * 1024 * 1024
MAX_SCENARIO = 20000
DEFAULT_SCENARIO = "General summary: what the document is, its key points, decisions required and action items."
OVERHEAD = 1024 * 1024
ID_RE = re.compile(r"[0-9a-f]{8}")
FAILED = ("failed", "error", "needs_opus", "cancelled")
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
       "form-action 'none'; frame-ancestors 'none'; base-uri 'none'")


def _queue_standby():
    import queue_config
    return queue_config.standby()


def _queue_frozen():
    import queue_config
    return queue_config.standby() or queue_config.draining()


def valid_id(s):
    return bool(ID_RE.fullmatch(s or ""))


def safe_name(name):
    base = re.split(r"[\\/]", name or "")[-1]
    base = "".join(c for c in base if c.isprintable())[:120].strip()
    return base if base.lower().endswith(".pdf") and len(base) > 4 else "document.pdf"


def queue_rows():
    """docsum rows from the queue state file: {id, label, status}. No other job data is read."""
    import queue_config
    jobs = (queue_config.load_queue_state(STATE) or {}).get("jobs") or []
    if not isinstance(jobs, list):
        return []
    return [{"id": j.get("id"), "label": j.get("label"), "status": j.get("status")}
            for j in jobs if isinstance(j, dict) and is_private_label(j.get("label"))]


def job_done(d):
    try:
        with open(os.path.join(d, "status.json")) as f:
            return json.load(f).get("ok") is True
    except Exception:
        return False


def served_models():
    import plex_bypass
    return plex_bypass.served_model_ids()


def enqueue_job(job, model):
    """Same enqueue command the doc-summary CLI builds; its output is discarded (it carries only queue ids anyway)."""
    from importlib.machinery import SourceFileLoader
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "doc-summary")
    spec = importlib.util.spec_from_loader("doc_summary_cli", SourceFileLoader("doc_summary_cli", path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    r = subprocess.run(mod.build_enqueue_cmd(job, model, None), capture_output=True, text=True)
    return r.returncode == 0


ACTIVE = ("running", "pending", "queued", "paused")
CLEAR_URL = None   # set below from queue_config.queue_api()


def clear_row(qid):
    """Dashboard clear for a finished/resolved row; best effort."""
    import urllib.request
    try:
        import queue_config
        url = CLEAR_URL or (queue_config.queue_api()[0] + "/api/runs/%s/clear")
        urllib.request.urlopen(urllib.request.Request(url % qid, data=b"", method="POST",
                                                      headers=queue_config.queue_api_headers()), timeout=5).read()
    except Exception:
        pass


def dispose_row(jid, rows=None, run=None, clear=None, done=False, wait=30, sleep=time.sleep):
    """Remove the queue row of docsum-<jid>: `cancel --force` ONLY for rows still active (running/queued/paused);
    `resolve` for failed-ish rows; finished rows are never stopped (that would leave a failed force_stopped row).
    Non-active rows are then cleared via the dashboard. Only the opaque queue id reaches the command line."""
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    clear = clear or clear_row
    rows = rows or queue_rows
    if done:       # output is final: the runner row is only finishing up, so let it exit instead of force-stopping it
        for _ in range(int(wait)):
            if not any(r["label"] == "docsum-" + jid and r["status"] in ACTIVE for r in rows()):
                break
            sleep(1)
        else:
            return
    for r in rows():
        if r["label"] != "docsum-" + jid or not r["id"]:
            continue
        if r["status"] in ACTIVE:
            run([sys.executable, QUEUE, "cancel", "--force", str(r["id"])])
            continue
        if r["status"] in FAILED:
            run([sys.executable, QUEUE, "resolve", str(r["id"])])
        clear(str(r["id"]))


READ_DEADLINE = 120.0


class Config:
    def __init__(self, root=PRIVATE_ROOT, rows=queue_rows, served=served_models, enqueue=enqueue_job,
                 resolve=dispose_row, max_pdf=MAX_PDF, log=None, now=time.time,
                 read_deadline=READ_DEADLINE):
        self.root, self.rows, self.served, self.enqueue, self.resolve = root, rows, served, enqueue, resolve
        self.max_pdf, self.now, self.read_deadline = max_pdf, now, read_deadline
        self.log = log or (lambda line: print(line, file=sys.stderr, flush=True))


MAX_CONCURRENT_UPLOADS = 2


def make_handler(cfg):
    uploads = threading.BoundedSemaphore(MAX_CONCURRENT_UPLOADS)

    class Handler(BaseHTTPRequestHandler):
        server_version = "docsum-ui"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = 30              # slow-loris guard: idle sockets are dropped

        def client_ip(self):
            return self.client_address[0]

        def log_message(self, *a, **k):  # request lines carry paths/queries: never print them
            pass

        def log_request(self, *a, **k):
            pass

        def log_error(self, *a, **k):
            pass

        def _log(self, route, status, size, jid="-"):
            if route == "jobs" and status == 200:      # the page polls this every 3 s: keep the log small
                return
            cfg.log("%s route=%s status=%d bytes=%d job=%s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), route, status, size, jid))

        def _send(self, status, body, ctype, route, jid="-", extra=None):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
            self._log(route, status, len(body), jid)

        def _json(self, status, obj, route, jid="-"):
            self._send(status, json.dumps(obj), "application/json", route, jid)

        def _err(self, status, code, route, jid="-"):
            self._json(status, {"error": code}, route, jid)

        def _guarded(self, method):
            bad = docsum_ui_guard.guard(self.client_ip(), self.headers, method)
            if bad:
                self.close_connection = True
                self._err(bad[0], bad[1], "guard")
                return False
            return True

        def do_GET(self):
            if not self._guarded("GET"):
                return
            path = self.path.split("?", 1)[0]
            if path == "/":
                return self._send(200, page_html(), "text/html; charset=utf-8", "page")
            if path == "/api/jobs":
                return self._json(200, {"jobs": list_jobs(cfg.root, cfg.rows(), cfg.now())}, "jobs")
            m = re.fullmatch(r"/job/([^/]+)/(pdf|md)", path)
            if m:
                return self._download(m.group(1), m.group(2))
            self._err(404, "E_NOT_FOUND", "other")

        def _download(self, jid, kind):
            if not valid_id(jid):
                return self._err(400, "E_BAD_ID", "download")
            p = os.path.join(cfg.root, jid, "summary." + kind)
            if not os.path.isfile(p) or os.path.islink(p):
                return self._err(404, "E_NOT_FOUND", "download", jid)
            with open(p, "rb") as f:
                data = f.read()
            try:
                with open(os.path.join(cfg.root, jid, "meta.json")) as f:
                    meta = json.load(f)
            except Exception:
                meta = {}
            meta = meta if isinstance(meta, dict) else {}
            ctype = "application/pdf" if kind == "pdf" else "text/markdown; charset=utf-8"
            self._send(200, data, ctype, "download", jid,
                       {"Content-Disposition": docsum_ui_name.download_filename(
                           meta.get("name"), jid, kind)})

        def do_POST(self):
            if not self._guarded("POST"):
                return
            path = self.path.split("?", 1)[0]
            if path in ("/api/upload",) or path.startswith("/api/delete/"):
                if _queue_frozen():   # standby copy of the Mac queue, or draining for the fallback: touch no queue row
                    return self._err(503, "E_STANDBY" if _queue_standby() else "E_DRAINING", "frozen")
            if path == "/api/upload":
                return self._upload()
            m = re.fullmatch(r"/api/delete/([^/]+)", path)
            if m:
                return self._delete(m.group(1))
            m = re.fullmatch(r"/api/rename/([^/]+)", path)
            if m:
                return self._rename(m.group(1))
            self._err(404, "E_NOT_FOUND", "other")

        def _read_body(self, n):
            """The request body, or None when it is not complete within cfg.read_deadline seconds overall (a stalled or
            drip-feeding client must not hold an upload slot)."""
            end, chunks, left = time.monotonic() + cfg.read_deadline, [], n
            while left > 0:
                remain = end - time.monotonic()
                if remain <= 0:
                    return None
                self.connection.settimeout(min(30.0, remain))
                try:
                    c = self.rfile.read1(min(65536, left))
                except OSError:
                    return None
                if not c:
                    return None
                chunks.append(c)
                left -= len(c)
            return b"".join(chunks)

        def _upload(self):
            try:
                n = int(self.headers.get("Content-Length") or "")
            except ValueError:
                return self._err(411, "E_BAD_UPLOAD", "upload")
            if n <= 0 or n > cfg.max_pdf + OVERHEAD:
                self.close_connection = True
                return self._err(413, "E_TOO_BIG", "upload")
            if not uploads.acquire(blocking=False):      # bound memory: at most 2 request bodies held at once
                self.close_connection = True
                return self._err(429, "E_BUSY", "upload")
            try:
                body = self._read_body(n)
                if body is None:
                    self.close_connection = True
                    return self._err(408, "E_TIMEOUT", "upload")
                try:
                    parts = parse_multipart(body, self.headers.get("Content-Type"))
                except ValueError:
                    return self._err(400, "E_BAD_UPLOAD", "upload")
                del body                                   # the parsed parts hold the data; drop the raw copy
                self._handle_parts(parts)
            finally:
                uploads.release()

        def _handle_parts(self, parts):
            f = parts.get("file") or {}
            data = f.get("data") or b""
            if len(data) > cfg.max_pdf:
                return self._err(413, "E_TOO_BIG", "upload")
            if not data.startswith(b"%PDF-"):
                return self._err(400, "E_NOT_PDF", "upload")
            scenario = (parts.get("scenario") or {}).get("data", b"").decode("utf-8", "replace")
            if not scenario.strip():                     # scenario is optional: fall back to a general summary
                scenario = DEFAULT_SCENARIO
            if len(scenario) > MAX_SCENARIO:
                return self._err(400, "E_SCENARIO_TOO_LONG", "upload")
            try:                                         # no model choice: use whatever Darkbloom has loaded
                model = docsum_cli.current_model(cfg.served)
            except ValueError:
                return self._err(503, "E_MODELS_UNAVAILABLE", "upload")
            ref_name = (parts.get("name") or {}).get("data", b"").decode("utf-8", "replace")
            os.makedirs(cfg.root, mode=0o700, exist_ok=True)
            os.chmod(cfg.root, 0o700)
            tmp = tempfile.mkdtemp(prefix=".up-", dir=cfg.root)
            try:
                src = os.path.join(tmp, safe_name(f.get("filename")))
                with open(src, "wb") as fh:
                    os.fchmod(fh.fileno(), 0o600)
                    fh.write(data)
                job = docsum_cli.prepare_job(src, scenario, cfg.root)
                if docsum_ui_name.clean_name(ref_name):
                    docsum_ui_name.set_name(job["dir"], ref_name)
            except Exception:
                return self._err(500, "E_PREPARE", "upload")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            try:
                ok = cfg.enqueue(job, model)
            except Exception:
                ok = False
            if not ok:
                shutil.rmtree(job["dir"], ignore_errors=True)
                return self._err(502, "E_ENQUEUE", "upload", job["job_id"])
            self._json(200, {"id": job["job_id"]}, "upload", job["job_id"])

        def _rename(self, jid):
            if not valid_id(jid):
                return self._err(400, "E_BAD_ID", "rename")
            try:
                n = int(self.headers.get("Content-Length") or "")
            except ValueError:
                return self._err(411, "E_BAD_UPLOAD", "rename")
            if n <= 0 or n > 4096:
                self.close_connection = True
                return self._err(413, "E_TOO_BIG", "rename")
            body = self._read_body(n)
            if body is None:
                self.close_connection = True
                return self._err(408, "E_TIMEOUT", "rename")
            try:
                raw = json.loads(body.decode("utf-8")).get("name", "")
                if not isinstance(raw, str):
                    raise ValueError
            except Exception:
                return self._err(400, "E_BAD_UPLOAD", "rename")
            d = os.path.join(cfg.root, jid)
            if os.path.islink(d) or not os.path.isdir(d):
                return self._err(404, "E_NOT_FOUND", "rename", jid)
            self._json(200, {"id": jid, "name": docsum_ui_name.set_name(d, raw)}, "rename", jid)

        def _delete(self, jid):
            if not valid_id(jid):
                return self._err(400, "E_BAD_ID", "delete")
            d = os.path.join(cfg.root, jid)
            if os.path.islink(d) or not os.path.isdir(d):
                return self._err(404, "E_NOT_FOUND", "delete", jid)
            try:                      # stop/remove the queue row first so a live runner cannot keep writing
                cfg.resolve(jid, done=job_done(d))
            except Exception:
                pass
            shutil.rmtree(d, ignore_errors=True)
            self._json(200, {"deleted": jid}, "delete", jid)

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # default prints a traceback and the client address
        self.cfg.log("%s route=error exc=%s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), sys.exc_info()[0].__name__))


def make_server(cfg, host="127.0.0.1", port=0):
    srv = Server((host, port), make_handler(cfg))
    srv.cfg = cfg
    return srv


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("DOCSUM_UI_PORT", DEFAULT_PORT)))
    ap.add_argument("--host", default=os.environ.get("DOCSUM_UI_HOST", "0.0.0.0"))
    a = ap.parse_args(argv)
    srv = make_server(Config(), a.host, a.port)
    print("%s route=start port=%d" % (time.strftime("%Y-%m-%dT%H:%M:%S"), a.port), file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
