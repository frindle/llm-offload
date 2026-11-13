#!/usr/bin/env python3
"""prefix-cache-trial -- replay a saved mini-swe-agent trajectory against a CANDIDATE OpenAI-compatible endpoint and measure
time-to-first-token (TTFT) per step, to see whether the server reuses the KV cache of the shared prompt prefix.

READ-ONLY by construction: it only POSTs /v1/chat/completions with max_tokens=1 (plus an optional GET /v1/models), never
loads/unloads/stops anything, never touches the queue, a run dir or Darkbloom. It REFUSES the live Darkbloom endpoint
(127.0.0.1:8000 or the base_url in ~/.darkbloom/local.json) unless --allow-darkbloom is given. The API key is read from a file
(--key-file, default none = no auth header), never from argv or the environment, and is never printed.

  prefix-cache-trial.py --traj RUN/foo.A1.traj.json --url http://HOST:PORT/v1 --model NAME [--key-file F]
        [--ctx raw|default|stable] [--steps 12] [--repeat 1] [--out report.json] [--dry-run]

Each step i replays the history the agent saw before its i-th assistant turn (messages[:k], the same bytes a real run would send),
streams the reply and records the seconds to the first content/reasoning token. `--ctx` picks how the history is rewritten first:
  raw      the trajectory messages as stored (what a no-context-control run sends)
  default  mini_ctx shrink_obs as shipped (per-turn shrinking: the prefix changes every turn)
  stable   mini_ctx with stable_prefix=true (shrinking in blocks: the prefix is append-only between block steps)
Needs `mini_ctx` (run it with ~/.ollama-dispatch/mini-venv/bin/python) for default/stable.

Reading the result: with working prefix caching TTFT per step stays roughly flat (only the new suffix is prefilled) and the server
reports cached tokens (usage.prompt_tokens_details.cached_tokens, when it sends usage); without it TTFT grows with the prompt.
Verdict line: ratio = median TTFT of the last third of steps / median of the first third, divided by the prompt-size growth over the
same steps. ratio < 0.5 -> prefix cache is effective; > 0.8 -> not. `--ctx default` vs `--ctx stable` on a caching server shows how
much the stable_prefix mode is worth.
"""
import argparse
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "mini_ext"))
DARKBLOOM = {"127.0.0.1:8000", "localhost:8000"}


def endpoint_keys(url):
    """{(address, port)} an URL points at, normalised so spelling cannot dodge the guard: case, userinfo (`u@`), brackets, default ports,
    numeric hosts (127.1, 2130706433), IPv4-mapped IPv6, and name resolution (localhost, a hostname for the same machine). Every loopback /
    wildcard address (127/8, ::1, 0.0.0.0, ::) collapses to ("loopback", port): a server on 0.0.0.0:8000 also answers on 127.0.0.1:8000."""
    import ipaddress
    import socket
    import urllib.parse
    u = (url or "").strip()
    sp = urllib.parse.urlsplit(u if "//" in u else "//" + u)
    host = (sp.hostname or "").lower().rstrip(".")
    try:
        port = sp.port
    except ValueError:
        port = None
    port = port or (443 if sp.scheme == "https" else 80)
    if not host:
        return set()
    try:
        infos = {ai[4][0].split("%")[0] for ai in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)}
    except (OSError, UnicodeError):
        return {(host, port)}
    keys = set()
    for a in infos:
        try:
            ip = ipaddress.ip_address(a)
            ip = ip.ipv4_mapped or ip if isinstance(ip, ipaddress.IPv6Address) else ip
            keys.add(("loopback" if ip.is_loopback or ip.is_unspecified else str(ip), port))
        except ValueError:
            keys.add((a, port))
    return keys or {(host, port)}


def darkbloom_keys():
    urls = ["http://" + h for h in DARKBLOOM]
    try:
        urls.append(json.loads(Path("~/.darkbloom/local.json").expanduser().read_text()).get("base_url", ""))
    except (OSError, ValueError):
        pass
    if os.environ.get("DARKBLOOM_BASE_URL"):
        urls.append(os.environ["DARKBLOOM_BASE_URL"])
    return set().union(*(endpoint_keys(u) for u in urls if u))


def is_darkbloom(url):
    return bool(endpoint_keys(url) & darkbloom_keys())


def load_messages(traj):
    msgs = json.loads(Path(traj).read_text())["messages"]
    return [{k: v for k, v in m.items() if k not in ("extra",)} for m in msgs]


def histories(msgs, ctx, steps):
    """[(step, messages)] -- the history before each assistant turn, rewritten per --ctx."""
    cfg = None
    if ctx != "raw":
        import mini_ctx
        cfg = mini_ctx.CtxConfig(model_name="x", stable_prefix=(ctx == "stable"), ceiling=False)
    out = []
    for i, m in enumerate(msgs):
        if m.get("role") == "assistant":
            h = msgs[:i]
            out.append((len(out) + 1, mini_ctx.shrink_messages(h, cfg) if cfg else [dict(x) for x in h]))
    return out[:steps] if steps else out


def clean(h):
    """Chat-completions-safe copy: tool_calls and tool messages are kept, vendor fields dropped."""
    keep = ("role", "content", "tool_calls", "tool_call_id", "name")
    return [{k: v for k, v in m.items() if k in keep} for m in h]


def stream_ttft(url, model, messages, key, timeout):
    body = json.dumps({"model": model, "messages": messages, "max_tokens": 1, "temperature": 0, "stream": True,
                       "stream_options": {"include_usage": True}}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/chat/completions", data=body, method="POST",
                                 headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + key} if key else {})})
    t0, first, usage = time.time(), None, {}
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            ln = raw.decode("utf-8", "replace").strip()
            if not ln.startswith("data:") or ln == "data: [DONE]":
                continue
            try:
                ev = json.loads(ln[5:])
            except ValueError:
                continue
            for c in ev.get("choices") or []:
                d = c.get("delta") or {}
                if first is None and (d.get("content") or d.get("reasoning_content") or d.get("reasoning") or c.get("finish_reason")):
                    first = time.time() - t0
            usage = ev.get("usage") or usage
    return (first if first is not None else time.time() - t0), usage


def verdict(rows):
    n = len(rows)
    if n < 6:
        return "too few steps for a verdict (need >= 6)"
    k = max(2, n // 3)
    head, tail = rows[:k], rows[-k:]
    t = statistics.median(r["ttft"] for r in tail) / max(statistics.median(r["ttft"] for r in head), 1e-6)
    g = statistics.median(r["est_tokens"] for r in tail) / max(statistics.median(r["est_tokens"] for r in head), 1)
    ratio = t / max(g, 1e-6)
    cached = [r["cached"] for r in rows if r.get("cached") is not None]
    word = "EFFECTIVE" if ratio < 0.5 else "NOT effective" if ratio > 0.8 else "inconclusive"
    return (f"prefix cache {word}: TTFT grew x{t:.2f} while the prompt grew x{g:.2f} (ratio {ratio:.2f})"
            + (f"; server-reported cached tokens {sum(cached)} of {sum(r['est_tokens'] for r in rows if r.get('cached') is not None)} est." if cached else "; server reports no cached-token counts"))


def run(a, post=stream_ttft):
    msgs = load_messages(a.traj)
    hs = histories(msgs, a.ctx, a.steps)
    key = Path(a.key_file).expanduser().read_text().strip() if a.key_file else ""
    rows = []
    for step, h in hs:
        h = clean(h)
        est = sum(len(json.dumps(m)) for m in h) // 3
        best = None
        for _ in range(a.repeat):
            ttft, usage = post(a.url, a.model, h, key, a.timeout)
            best = (ttft, usage) if best is None or ttft < best[0] else best
        ttft, usage = best
        det = (usage.get("prompt_tokens_details") or {}) if isinstance(usage, dict) else {}
        rows.append({"step": step, "msgs": len(h), "est_tokens": est, "prompt_tokens": usage.get("prompt_tokens"),
                     "cached": det.get("cached_tokens"), "ttft": round(ttft, 3)})
        print(f"step {step:3d}  msgs={len(h):3d}  ~{est:6d} tok  prompt_tokens={rows[-1]['prompt_tokens']}  cached={rows[-1]['cached']}  TTFT={ttft:.2f}s", flush=True)
    v = verdict(rows)
    print(f"VERDICT ({a.ctx}): {v}")
    if a.out:
        Path(a.out).write_text(json.dumps({"url": a.url, "model": a.model, "ctx": a.ctx, "rows": rows, "verdict": v}, indent=1))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--traj"), ap.add_argument("--url"), ap.add_argument("--model")
    ap.add_argument("--key-file"), ap.add_argument("--out")
    ap.add_argument("--ctx", choices=("raw", "default", "stable"), default="raw")
    ap.add_argument("--steps", type=int, default=12, help="replay at most this many assistant turns (0 = all)")
    ap.add_argument("--repeat", type=int, default=1, help="send each step this many times, keep the fastest (2 shows warm-cache TTFT)")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--allow-darkbloom", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print the plan (steps, sizes), send nothing")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if not (a.traj and a.url and a.model):
        ap.error("--traj, --url and --model are required")
    host = a.url.split("//", 1)[-1].split("/", 1)[0]
    if is_darkbloom(a.url) and not a.allow_darkbloom:
        print(f"refusing: {host} is the live Darkbloom endpoint; give the CANDIDATE server's URL (or --allow-darkbloom)", file=sys.stderr)
        return 2
    if a.dry_run:
        for step, h in histories(load_messages(a.traj), a.ctx, a.steps):
            print(f"step {step:3d}  msgs={len(h):3d}  ~{sum(len(json.dumps(m)) for m in clean(h)) // 3:6d} tok")
        print(f"dry run: would POST {len(histories(load_messages(a.traj), a.ctx, a.steps)) * a.repeat} max_tokens=1 requests to {a.url}")
        return 0
    run(a)
    return 0


def self_test():
    import http.server
    import tempfile

    class H(http.server.BaseHTTPRequestHandler):
        seen = []

        def log_message(self, *x):
            pass

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            H.seen.append((self.path, req["max_tokens"], req["stream"], self.headers.get("Authorization")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            time.sleep(0.01 + len(json.dumps(req["messages"])) / 4e6)
            for ev in ({"choices": [{"delta": {"content": "x"}}]}, {"choices": [], "usage": {"prompt_tokens": 9, "prompt_tokens_details": {"cached_tokens": 5}}}):
                self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
    with tempfile.TemporaryDirectory() as d:
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
        for i in range(10):
            msgs += [{"role": "assistant", "content": f"a{i}", "tool_calls": [{"id": str(i), "type": "function", "function": {"name": "bash", "arguments": "{}"}}], "extra": {"x": 1}},
                     {"role": "tool", "tool_call_id": str(i), "content": "o" * 500}]
        tj, kf, out = Path(d, "t.traj.json"), Path(d, "key"), Path(d, "r.json")
        tj.write_text(json.dumps({"messages": msgs}))
        kf.write_text("SECRETKEY\n")
        assert main(["--traj", str(tj), "--url", "http://127.0.0.1:8000/v1", "--model", "m"]) == 2, "Darkbloom refused"
        assert not H.seen
        for bad in ("http://localhost:8000/v1", "http://LOCALHOST:8000/v1", "http://[::1]:8000/v1", "http://0.0.0.0:8000/v1", "http://127.0.0.1:8000",
                    "http://u@127.0.0.1:8000/v1", "http://user:pw@LocalHost:8000", "http://127.1:8000/v1", "127.0.0.1:8000/v1", "http://2130706433:8000/v1",
                    "http://[::ffff:127.0.0.1]:8000/v1", "http://127.0.0.2:8000/v1", "http://localhost.:8000/v1", "HTTP://127.0.0.1:8000/v1"):
            assert is_darkbloom(bad), bad
            assert main(["--traj", str(tj), "--url", bad, "--model", "m"]) == 2, bad
        for good in ("http://127.0.0.1:8001/v1", "http://localhost:11434/v1", "http://127.0.0.1:80/v1", "http://example.invalid:8000/v1"):
            assert not is_darkbloom(good), good
        assert not is_darkbloom(url)  # the test server's own ephemeral port
        assert main(["--traj", str(tj), "--url", url, "--model", "m", "--dry-run"]) == 0 and not H.seen, "dry run sends nothing"
        a = argparse.Namespace(traj=str(tj), url=url, model="m", key_file=str(kf), out=str(out), ctx="raw", steps=0, repeat=2, timeout=10)
        rows = run(a)
        assert len(rows) == 10 and len(H.seen) == 20 and all(p == "/v1/chat/completions" and mt == 1 and st is True and au == "Bearer SECRETKEY" for p, mt, st, au in H.seen)
        assert rows[0]["cached"] == 5 and json.loads(out.read_text())["verdict"] and "SECRETKEY" not in out.read_text()
        try:
            import mini_ctx
        except ImportError:
            print("(mini_ctx not importable with this python; default/stable skipped)")
        else:
            for ctx in ("default", "stable"):
                assert len(histories(msgs, ctx, 0)) == 10
    srv.shutdown()
    flat = verdict([{"ttft": 0.5, "est_tokens": 1000 * (i + 1), "cached": None} for i in range(9)])
    grow = verdict([{"ttft": 0.5 * (i + 1), "est_tokens": 1000 * (i + 1), "cached": None} for i in range(9)])
    assert "EFFECTIVE" in flat and "NOT effective" in grow, (flat, grow)
    print("prefix-cache-trial self-test ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
