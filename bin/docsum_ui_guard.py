"""Request guard for the docsum web UI. No token, cookie or pairing, and no client-IP/tunnel/Host rejection: the trust boundary
is the LAN or Cloudflare Access in front of the queue-API proxy. The only check is that a POST is same-origin (Origin matches Host)."""
from urllib.parse import urlparse


def same_origin(headers):
    origin, host = headers.get("Origin"), headers.get("Host")
    if not origin or not host:
        return False
    return urlparse(origin).netloc.lower() == host.strip().lower()


def guard(client_ip, headers, method):
    """None when the request may proceed, else (403, code). Only POST is checked (same-origin); client_ip is ignored."""
    if method == "POST" and not same_origin(headers):
        return (403, "E_CROSS_ORIGIN")
    return None
