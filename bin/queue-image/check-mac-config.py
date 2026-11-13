#!/usr/bin/env python3
"""Compare the values the code used to hard-code with what the config layer returns now.

Prints: key, old value, new value, SAME or DIFF. Exit 1 if any DIFF. Reads config only; it does not
run the queue. The old values live outside git in ~/.config/ollama-queue/old-values.toml
(override with $QUEUE_OLD_VALUES) because they are the operator's private addresses.
"""
import os
import sys
import tomllib
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import queue_config  # noqa: E402

OLD = Path(os.environ.get("QUEUE_OLD_VALUES") or Path.home() / ".config/ollama-queue/old-values.toml")


def bonsai_new():
    url = queue_config.get_str("bonsai.url", "", env="BONSAI_SERVER_URL")
    if url:
        return url
    f = Path.home() / ".config/ollama-queue/bonsai-host"
    return f.read_text().strip() if f.is_file() else ""


def main():
    if not OLD.is_file():
        print(f"no old-values file at {OLD}", file=sys.stderr)
        return 2
    old = tomllib.loads(OLD.read_text())
    rows = []
    for key in ("ollama.unraid_url", "ollama.studio_url", "search.searxng_url", "vault.url", "darkbloom.base_url",
                "diagnostics.unraid_graphql_url", "bakeoff.driver", "operator.name", "operator.handle"):
        rows.append((key, old.get(key, ""), queue_config.get_str(key, "")))
    rows.append(("darkbloom.upstream_hosts", old["darkbloom.upstream_hosts"],
                 ",".join(queue_config.get_list("darkbloom.upstream_hosts", []))))
    # ollama-worker derives its "is this Unraid" substring from ollama.unraid_url.
    rows.append(("unraid host substring (derived)", old["unraid_hosts_substring"],
                 urllib.parse.urlsplit(queue_config.get_str("ollama.unraid_url", "")).hostname or ""))
    rows.append(("state_dir", old["state_dir"], str(queue_config.state_dir())))
    bonsai_file = Path.home() / ".config/ollama-queue/bonsai-host"
    rows.append(("bonsai url (file or config)", bonsai_file.read_text().strip() if bonsai_file.is_file() else "", bonsai_new()))
    bad = 0
    for key, o, n in rows:
        same = str(o) == str(n)
        bad += not same
        print(f"{key}\told={o!r}\tnew={n!r}\t{'SAME' if same else 'DIFF'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
