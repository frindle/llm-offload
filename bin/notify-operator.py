#!/usr/bin/env python3
"""notify-operator -- one alert to the operator, without Claude in the loop.

Pushover when Keychain has `pushover-token` + `pushover-user-key` (generic
passwords; never printed), else a macOS desktop notification. Every alert is also
appended to ~/bin/ollama-queue-logs/notify-operator.jsonl so it is never lost.

  notify-operator.py "title" "message" [--url URL] [--dedupe-key K --dedupe-s 3600]

--dedupe-key suppresses a repeat of the same key within --dedupe-s seconds (the
stall detector / reaper must send ONE alert, not one per tick). Exit 0 always.
"""
import os as _qc_os, sys as _qc_sys
_qc_dir = _qc_os.path.dirname(_qc_os.path.realpath(__file__))
if _qc_dir not in _qc_sys.path:
    _qc_sys.path.append(_qc_dir)
import queue_config
import argparse
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

LOG = queue_config.state_dir() / "ollama-queue-logs" / "notify-operator.jsonl"
DEDUPE = Path.home() / ".ollama-dispatch" / "notify-dedupe.json"


def _keychain(service):
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except Exception:
        return None


def _deduped(key, window, now, path=DEDUPE):
    if not key:
        return False
    try:
        seen = json.loads(path.read_text())
    except (OSError, ValueError):
        seen = {}
    if now - float(seen.get(key, 0)) < window:
        return True
    seen = {k: v for k, v in seen.items() if now - float(v) < 7 * 86400}
    seen[key] = now
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(seen))
    except OSError:
        pass
    return False


def notify(title, message, url=None, dedupe_key=None, dedupe_s=3600, now=None,
           sender=None):
    """Returns the channel used ('pushover' | 'desktop' | 'deduped' | 'none')."""
    now = time.time() if now is None else now
    if _deduped(dedupe_key, dedupe_s, now):
        return "deduped"
    channel = "none"
    if queue_config.standby():
        channel = "standby"           # the standby container never alerts; the log line below still records it
    elif sender is not None:
        channel = sender(title, message, url)
    else:
        tok, usr = _keychain("pushover-token"), _keychain("pushover-user-key")
        if tok and usr:
            try:
                data = {"token": tok, "user": usr, "title": title[:250],
                        "message": message[:1000]}
                if url:
                    data["url"] = url
                urllib.request.urlopen("https://api.pushover.net/1/messages.json",
                                       urllib.parse.urlencode(data).encode(), timeout=15)
                channel = "pushover"
            except Exception:
                channel = "none"
        if channel == "none":
            try:
                safe = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')[:400]
                subprocess.run(["osascript", "-e",
                                f'display notification "{safe(message)}" with title "{safe(title)}"'],
                               capture_output=True, timeout=10)
                channel = "desktop"
            except Exception:
                pass
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(json.dumps({"ts": now, "title": title, "message": message,
                                "url": url, "channel": channel, "key": dedupe_key}) + "\n")
    except OSError:
        pass
    return channel


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("title")
    ap.add_argument("message")
    ap.add_argument("--url")
    ap.add_argument("--dedupe-key")
    ap.add_argument("--dedupe-s", type=int, default=3600)
    a = ap.parse_args(argv)
    print(notify(a.title, a.message, a.url, a.dedupe_key, a.dedupe_s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
