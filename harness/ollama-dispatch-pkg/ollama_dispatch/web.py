"""Optional, provider-agnostic web search/fetch for the worker.

Search is OFF unless configured. There is no hardcoded provider and no secret
store coupling: a backend is enabled purely through env vars, and the API key
(if any) is read from the environment or from a file path you point at.

Configure a search backend with:

    OLLAMA_DISPATCH_SEARCH_URL     an endpoint that accepts ?q=<query> and
                                   returns JSON or text (e.g. a self-hosted
                                   SearXNG instance: http://searx.example.lan/search?format=json)
    OLLAMA_DISPATCH_SEARCH_KEY     optional bearer token value, OR
    OLLAMA_DISPATCH_SEARCH_KEY_FILE  path to a file containing the token

Fetch (URL -> main text) works with no configuration; it degrades gracefully if
`trafilatura` is not installed (falls back to a naive HTML strip).
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request

WEB_TIMEOUT_S = 20
WEB_FETCH_MAX_CHARS = 5000
SEARCH_MAX_CHARS = 4000


def _search_key() -> str:
    key = os.environ.get("OLLAMA_DISPATCH_SEARCH_KEY", "")
    if key:
        return key
    path = os.environ.get("OLLAMA_DISPATCH_SEARCH_KEY_FILE")
    if path and os.path.exists(path):
        try:
            return open(path).read().strip()
        except OSError:
            return ""
    return ""


def search(query: str) -> str:
    """Query the configured search backend. Returns a text block, or a clear
    'not configured' message so a run never hard-fails on a missing backend."""
    base = os.environ.get("OLLAMA_DISPATCH_SEARCH_URL")
    if not base:
        return (
            "(web_search is not configured -- set OLLAMA_DISPATCH_SEARCH_URL to "
            "enable it, e.g. a self-hosted SearXNG endpoint)"
        )
    sep = "&" if "?" in base else "?"
    url = f"{base}{sep}q={urllib.parse.quote(query)}"
    req = urllib.request.Request(url)
    key = _search_key()
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=WEB_TIMEOUT_S) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001 -- best-effort tool, never raises into the loop
        return f"(web_search error: {e})"
    # Best-effort: pull titles/snippets out of a SearXNG-style JSON response.
    try:
        data = json.loads(body)
        results = data.get("results", []) if isinstance(data, dict) else []
        lines = []
        for r in results[:8]:
            lines.append(f"- {r.get('title', '')}\n  {r.get('url', '')}\n  {r.get('content', '')}")
        return ("\n".join(lines) or "(no results)")[:SEARCH_MAX_CHARS]
    except json.JSONDecodeError:
        return body[:SEARCH_MAX_CHARS]


def fetch(url: str) -> str:
    """Fetch a URL and return its main text (best-effort extraction)."""
    if not url.startswith(("http://", "https://")):
        return "(web_fetch: only http(s) URLs are supported)"
    req = urllib.request.Request(url, headers={"User-Agent": "ollama-dispatch/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=WEB_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return f"(web_fetch error: {e})"

    try:
        import trafilatura  # type: ignore

        text = trafilatura.extract(raw) or ""
    except ImportError:
        text = re.sub(r"<script.*?</script>|<style.*?</style>", "", raw, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
    return text[:WEB_FETCH_MAX_CHARS] if text else "(no extractable text)"
