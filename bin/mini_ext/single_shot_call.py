#!/usr/bin/env python3
"""One chat completion through litellm with the SAME sampling as the mini overlay (configs/mini-local.yaml model.model_kwargs).

Run by mini-swe-runner.py single-shot repair under the mini venv's interpreter (litellm + pyyaml live there). Protocol:
stdin  = JSON {"model", "base_url", "messages", "overlay", "no_think", "timeout", optional "max_tokens"}
env    = MINI_KEY_FILE: a 0600 file with the endpoint key (read and deleted here; never in argv, stdin or env of other processes)
stdout = one line `@@SS@@ <json>` with {"content", "finish_reason", "usage": {"prompt_tokens", "completion_tokens"}, "secs"}
         or {"error": "<ExceptionType>: short text"}   (litellm may print other lines; only the marker line counts)
"""
import json
import os
import sys
import time


def merge(a, b):
    out = dict(a)
    for k, v in b.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def kwargs_for(overlay_text, no_think, timeout=None):
    import yaml
    kw = dict((yaml.safe_load(overlay_text).get("model") or {}).get("model_kwargs") or {})
    kw.setdefault("drop_params", True)
    if no_think:
        kw = merge(kw, {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}})
    if timeout:
        kw["timeout"] = min(float(kw.get("timeout", timeout)), float(timeout))
    return kw


def main():
    req = json.load(sys.stdin)
    kp, key = os.environ.pop("MINI_KEY_FILE", None), ""
    if kp:
        try:
            key = open(kp).read().strip()
        finally:
            try:
                os.unlink(kp)
            except OSError:
                pass
    try:
        import litellm
        kw = kwargs_for(open(req["overlay"]).read(), req.get("no_think"), req.get("timeout"))
        if req.get("max_tokens"):
            kw["max_tokens"] = int(req["max_tokens"])
        t0 = time.time()
        r = litellm.completion(model="openai/" + req["model"], messages=req["messages"], api_base=req["base_url"], api_key=key or None, **kw)
        ch = r.choices[0]
        u = getattr(r, "usage", None)
        out = {"content": ch.message.content or "", "finish_reason": ch.finish_reason,
               "usage": {"prompt_tokens": getattr(u, "prompt_tokens", 0) or 0, "completion_tokens": getattr(u, "completion_tokens", 0) or 0},
               "secs": round(time.time() - t0, 1)}
    except Exception as e:  # noqa: BLE001 -- the runner turns any failure into a rejected attempt
        msg = str(e).replace(key, "<key>") if key else str(e)
        out = {"error": f"{type(e).__name__}: {msg[:300]}"}
    print("@@SS@@ " + json.dumps(out), flush=True)


if __name__ == "__main__":
    main()
