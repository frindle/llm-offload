"""Reverse proxy of /docsum/* on the queue API to the private doc-summary UI (127.0.0.1:7690 unless config docsum.upstream /
QUEUE_DOCSUM_UPSTREAM names host:port: the UI is not in the queue image, so a containerised queue reaches it on another machine). Stdlib only.
Never logs or prints a path, query, header, filename or body; a document body is held in memory only for the one request."""
import http.client
import json
from urllib.parse import urlsplit

PREFIX = "/docsum"
UPSTREAM = ("127.0.0.1", 7690)
UPSTREAM_HOST = "%s:%d" % UPSTREAM
_DEFAULT = UPSTREAM
MAX_BODY = 110 * 1024 * 1024
CHUNK = 64 * 1024


def current_upstream():
    """(host, port) the proxy forwards to: config docsum.upstream ("host:port"; env QUEUE_DOCSUM_UPSTREAM) over the default. An
    unset, malformed or out-of-range value falls back to the default, never to a guess."""
    try:
        import queue_config
        raw = queue_config.get_str("docsum.upstream", "").strip()
    except Exception:
        return UPSTREAM
    host, sep, port = raw.rpartition(":")
    host = host.strip("[]")
    if not (sep and host and port.isdigit() and 0 < int(port) < 65536) or not all(c.isalnum() or c in ".-_" for c in host):
        return UPSTREAM
    return host, int(port)


def origin_ok(headers):
    origin = headers.get("Origin")
    if not origin:
        return True
    host = headers.get("Host")
    if not host:
        return False
    return urlsplit(origin).netloc.lower() == host.strip().lower()


def upstream_headers(headers, upstream=None):
    up_host = "%s:%d" % upstream if upstream else UPSTREAM_HOST
    result = {"Host": up_host}
    for key in ("Cookie", "Content-Type", "Content-Length", "Accept"):
        val = headers.get(key)
        if val is not None:
            result[key] = val
    origin = headers.get("Origin")
    if origin:
        result["Origin"] = "http://" + up_host
    return result


def proxy(h, conn_factory=http.client.HTTPConnection):
    h.close_connection = True
    # Strip the /docsum prefix to get the path for upstream
    p = h.path
    qmark = p.find("?")
    if qmark == -1:
        path_no_prefix = p[len(PREFIX):]
    else:
        path_no_prefix = p[len(PREFIX):qmark] + p[qmark:]
    # Exact /docsum redirect (308) - only when path before ? is exactly /docsum
    path_before_q = p.split("?")[0]
    if path_before_q == PREFIX:
        h.send_response(308)
        h.send_header("Location", "/docsum/")
        h.send_header("Content-Length", "0")
        h.end_headers()
        return
    # POST origin check
    if h.command == "POST":
        if not origin_ok(h.headers):
            body = json.dumps({"error": "E_CROSS_ORIGIN"}).encode()
            h.send_response(403)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)
            return
        cl_raw = h.headers.get("Content-Length")
        if cl_raw is None:
            body_len = 0
        elif not (cl_raw.isascii() and cl_raw.isdigit()):
            body = json.dumps({"error": "E_TOO_BIG"}).encode()
            h.send_response(413)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)
            return
        else:
            body_len = int(cl_raw)
        if body_len > MAX_BODY:
            body = json.dumps({"error": "E_TOO_BIG"}).encode()
            h.send_response(413)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)
            return
        if body_len > 0:
            raw_body = h.rfile.read(body_len)
        else:
            raw_body = None
    else:
        raw_body = None
    allowed = ("Content-Type", "Content-Disposition", "Cache-Control", "Set-Cookie",
               "Location", "Content-Security-Policy", "X-Content-Type-Options", "Referrer-Policy")
    conn = None
    try:
        up = current_upstream() if UPSTREAM == _DEFAULT else UPSTREAM   # a patched module constant (tests) still wins
        conn = conn_factory(*up, timeout=60)
        conn.request(h.command, path_no_prefix, body=raw_body, headers=upstream_headers(h.headers, up))
        resp = conn.getresponse()
        status = resp.status
        resp_body = resp.read()
        resp_headers = [(k, resp.getheader(k)) for k in allowed]
    except Exception:
        status, resp_body = 502, json.dumps({"error": "E_DOCSUM_DOWN"}).encode()
        resp_headers = [("Content-Type", "application/json")]
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    h.send_response(status)
    for k, v in resp_headers:
        if v is not None:
            h.send_header(k, v)
    h.send_header("Content-Length", str(len(resp_body)))
    h.end_headers()
    h.wfile.write(resp_body)
