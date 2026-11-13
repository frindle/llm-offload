"""Plex-automation bypass: forward a vetted chat-completion body to Darkbloom's loopback endpoint."""
import socket
import threading
import urllib.error
import urllib.request

import os as _qc_os, sys as _qc_sys
_qc_dir = _qc_os.path.dirname(_qc_os.path.realpath(__file__))
if _qc_dir not in _qc_sys.path:
    _qc_sys.path.append(_qc_dir)
import queue_config

UPSTREAM = queue_config.darkbloom_endpoint()[0].removesuffix("/v1") + "/v1/chat/completions"
TIMEOUT_S = 20
_SLOTS = threading.BoundedSemaphore(4)


def forward_chat(body, key, upstream=UPSTREAM, timeout=TIMEOUT_S):
    if not _SLOTS.acquire(blocking=False):
        return (503, b"busy")
    try:
        req = urllib.request.Request(upstream, data=body, method="POST")
        req.add_header("Authorization", "Bearer " + key)
        req.add_header("Content-type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.getcode() == 200:
                return (200, resp.read())
            else:
                return (502, b"upstream error")
    except (socket.timeout, TimeoutError):
        return (504, b"upstream timeout")
    except urllib.error.HTTPError as e:
        if 400 <= e.code <= 599 and e.code not in (401, 403, 500):
            return (e.code, b"upstream error")
        return (502, b"upstream error")
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            return (504, b"upstream timeout")
        return (502, b"upstream error")
    except Exception:
        return (502, b"upstream error")
    finally:
        _SLOTS.release()
