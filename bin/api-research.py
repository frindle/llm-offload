#!/usr/bin/env python3
"""api-research.py NAME PROMPT_FILE OUT_FILE

Web research on the paid Anthropic API (monthly credits), for questions that need live web pages.
~/bin/claude-api (claude --bare) has no web tools, so this calls the Messages API directly with the server tools
web_search_20260209 and web_fetch_20260209, handles pause_turn, and uses only the stdlib.

  NAME         label written to the burn log (same file and format as ~/bin/claude-api)
  PROMPT_FILE  the question(s); ask 2 or 3 per run (more runs out of tool calls)
  OUT_FILE     the answer text is written here; the sources list goes to OUT_FILE.sources.txt

Env (all optional): SEARCHES (default 15), FETCHES (10), FETCH_TOKENS (6000), MODEL (claude-sonnet-5-5),
BUDGET_USD (default 5: the loop stops once the estimated cost passes it), MAX_TOKENS (16000),
CLAUDE_API_MONTHLY_CAP (default 100, same as claude-api; a run is refused when spent + BUDGET_USD passes it).

Key: macOS Keychain (-a anthropic -s anthropic-api-key). Every request carries the workspace header.
Privacy: the prompt goes to an outside service. Name only public models, public repos and generic workload kinds.
"""
import datetime
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

LOG = os.path.expanduser("~/.local/state/claude-api/burn.tsv")
WS = "wrkspc_01LjwXsr8xXnmj8bJevosxaa"
URL = "https://api.anthropic.com/v1/messages"
# USD per million tokens (input, output); web search is billed per search.
PRICES = {"claude-sonnet-5-5": (2.00, 10.00), "claude-haiku-5-5": (0.10, 0.50), "claude-opus-5-5": (5.00, 25.00)}
SEARCH_USD = 0.01


def month_spent(cap_month=None):
    m = cap_month or datetime.date.today().strftime("%Y-%m")
    total = 0.0
    try:
        for line in open(LOG):
            p = line.rstrip("\n").split("\t")
            if len(p) >= 4 and p[0].startswith(m):
                try:
                    total += float(p[3])
                except ValueError:
                    pass
    except OSError:
        pass
    return total


def cost_of(model, usage):
    pin, pout = PRICES.get(model, max(PRICES.values()))
    u = usage or {}
    inp = u.get("input_tokens", 0) or 0
    out = u.get("output_tokens", 0) or 0
    cr = u.get("cache_read_input_tokens", 0) or 0
    cw = u.get("cache_creation_input_tokens", 0) or 0
    searches = ((u.get("server_tool_use") or {}).get("web_search_requests", 0)) or 0
    usd = (inp * pin + out * pout + cr * pin * 0.1 + cw * pin * 1.25) / 1e6 + searches * SEARCH_USD
    return usd


def add_usage(tot, usage):
    for k, v in (usage or {}).items():
        if isinstance(v, int):
            tot[k] = tot.get(k, 0) + v
        elif isinstance(v, dict):
            sub = tot.setdefault(k, {})
            for k2, v2 in v.items():
                if isinstance(v2, int):
                    sub[k2] = sub.get(k2, 0) + v2


def main(argv):
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 64
    name, prompt_file, out_file = argv[1:]
    model = os.environ.get("MODEL", "claude-sonnet-5-5")
    searches = int(os.environ.get("SEARCHES", "15"))
    fetches = int(os.environ.get("FETCHES", "10"))
    fetch_tokens = int(os.environ.get("FETCH_TOKENS", "6000"))
    budget = float(os.environ.get("BUDGET_USD", "5"))
    max_tokens = int(os.environ.get("MAX_TOKENS", "16000"))
    cap = float(os.environ.get("CLAUDE_API_MONTHLY_CAP", "100"))
    spent = month_spent()
    if spent + budget > cap:
        print(f"api-research: ${spent:.2f} spent this month; a ${budget} run would pass the ${cap} cap", file=sys.stderr)
        return 3
    prompt = open(prompt_file).read()
    try:
        key = subprocess.run(["security", "find-generic-password", "-a", "anthropic", "-s", "anthropic-api-key", "-w"],
                             capture_output=True, text=True, check=True).stdout.strip()
    except subprocess.CalledProcessError:
        print("api-research: no key in the Keychain (locked? security unlock-keychain)", file=sys.stderr)
        return 3
    tools = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": searches},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": fetches, "max_content_tokens": fetch_tokens},
    ]
    messages = [{"role": "user", "content": prompt}]
    total, est, turns = {}, 0.0, 0
    sources, texts = [], []
    rc = 0
    while True:
        turns += 1
        body = {"model": model, "max_tokens": max_tokens, "messages": messages, "tools": tools,
                "cache_control": {"type": "ephemeral"}}
        req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST", headers={
            "content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01",
            "anthropic-workspace-id": WS})
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            print(f"api-research: HTTP {e.code}: {e.read().decode(errors='replace')[:1500]}", file=sys.stderr)
            rc = 1
            break
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"api-research: request failed: {e}", file=sys.stderr)
            rc = 1
            break
        add_usage(total, resp.get("usage"))
        est += cost_of(model, resp.get("usage"))
        content = resp.get("content") or []
        for b in content:
            t = b.get("type")
            if t == "text":
                texts.append(b.get("text", ""))
            elif t == "web_search_tool_result" and isinstance(b.get("content"), list):
                for it in b["content"]:
                    if it.get("url"):
                        sources.append(f"search\t{it.get('url')}\t{it.get('title', '')}")
            elif t == "web_fetch_tool_result":
                c = b.get("content") or {}
                if isinstance(c, dict) and c.get("url"):
                    sources.append(f"fetch\t{c.get('url')}")
        stop = resp.get("stop_reason")
        if stop == "pause_turn":
            if est >= budget:
                print(f"api-research: budget ${budget} reached (est ${est:.2f}); stopping", file=sys.stderr)
                rc = 2
                break
            messages = [messages[0], {"role": "assistant", "content": content}]
            continue
        if stop == "max_tokens":
            print("api-research: hit max_tokens; answer may be cut", file=sys.stderr)
        break
    with open(out_file, "w") as f:
        f.write("\n".join(t for t in texts if t))
    with open(out_file + ".sources.txt", "w") as f:
        seen = set()
        for s in sources:
            if s not in seen:
                seen.add(s)
                f.write(s + "\n")
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(f"{datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\t{name}\t{model}\t{est:.4f}\n")
    st = (total.get("server_tool_use") or {})
    print(f"api-research: {name} cost ${est:.2f} (est; {turns} turns, {st.get('web_search_requests', 0)} searches, "
          f"{st.get('web_fetch_requests', 0)} fetches; month ${month_spent():.2f} of ${cap:.0f})", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
