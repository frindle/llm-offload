"""Plex-automation bypass: HTTP glue between the queue API Handler and the vetting/forwarding modules."""
import json
import os
import sys
import time

import plex_bypass as pb
import plex_bypass_upstream as pu

ROUTE = "/api/bypass/chat/completions"
TOKEN_FILE = os.path.expanduser("~/.config/plex-bypass/token")
KEY_FILE = os.path.expanduser("~/.darkbloom/local.json")


def _api_key(path=None):
    if path is None and os.environ.get("DARKBLOOM_API_KEY", "").strip():
        return os.environ["DARKBLOOM_API_KEY"].strip()
    try:
        with open(path or KEY_FILE, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "api_key" not in data:
            return ""
        return str(data["api_key"]).strip()
    except (OSError, ValueError, KeyError):
        return ""


def handle(h, token_file=None, key_file=None):
    start = time.time()
    path = h.path.split("?", 1)[0] if h.path else ""
    if path != ROUTE:
        status = 404
        body = b"not found"
    else:
        cl_str = h.headers.get("Content-Length", "0")
        try:
            cl = int(cl_str)
        except (ValueError, TypeError):
            status = 400
            body = b"bad request"
            cl = 0
        else:
            if cl < 0:
                status = 400
                body = b"bad request"
                cl = 0
            elif cl > pb.MAX_BODY:
                status = 413
                body = b"body too large"
                cl = 0
            else:
                raw = h.rfile.read(cl)
                status, body = pb.prepare_request(
                    h.headers.get("Authorization", ""), raw, pb.read_secret(token_file or TOKEN_FILE)
                )
                if status == 200:
                    status, body = pu.forward_chat(body, _api_key(key_file))

    ct = "application/json" if status == 200 else "text/plain"
    elapsed_ms = int((time.time() - start) * 1000)
    h.send_response(status)
    h.send_header("Content-Type", ct)
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)
    sys.stderr.write("[plex-bypass] POST {} -> {} ({} ms)\n".format(ROUTE, status, elapsed_ms))
    sys.stderr.flush()
