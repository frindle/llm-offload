#!/usr/bin/env python3
"""Darkbloom gets a repetition penalty it actually READS (2026-10-04).

Darkbloom (MLX) reads `repetition_penalty`, never llama-server's `repeat_penalty`, and
qwen3.6-35b-a3b-vl-mtp-mxfp8's generation_config sets none -- so every Darkbloom
dispatch decoded unpenalized near temperature 0: output_cap_loop authors (984db5b6a535,
06c2bf413cd7, 62dcd980447d, f5144683e2a0) and runaway review JSON that crashed
regate-47d71a149da5 / regate-0813211c0643.

Covers both senders: ollama-worker.py (call_ollama openai branch + call_openai_streaming)
and darkbloom_chat.py (the runners' chat()). Offline: the HTTP layer is faked.
--revert-check mutates each guard (WORKER_SRC / DBC_SRC env) and requires RED."""
import importlib.machinery, importlib.util, io, json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKER = Path(os.environ.get("WORKER_SRC") or HERE / "ollama-worker.py")
DBC = Path(os.environ.get("DBC_SRC") or HERE / "darkbloom_chat.py")
FAILS = []
PAIR = "qwen3.6-35b-a3b-vl-mtp-mxfp8"   # real profile: override 1.3 must beat the card 1.0


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def _load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def worker_payloads():
    os.environ["HOME"] = tempfile.mkdtemp(prefix="dbpen-home-")
    os.environ.pop("VAULT_TOKEN_FILE", None)
    m = _load(WORKER, "ow_dbpen")
    sent = []

    def fake_urlopen(req, timeout=None):
        sent.append(json.loads(req.data))
        if json.loads(req.data).get("stream"):
            body = (b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\n'
                    b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4}}\n\n'
                    b'data: [DONE]\n\n')
            r = _Resp(body)
        else:
            r = _Resp(json.dumps({"choices": [{"message": {"role": "assistant", "content": "hi"},
                                               "finish_reason": "stop"}],
                                  "usage": {"prompt_tokens": 3, "completion_tokens": 1}}).encode())
        r.status = 200
        r.headers = {}
        return r

    m.urllib.request.urlopen = fake_urlopen
    msgs = [{"role": "user", "content": "x"}]
    try:
        m.call_ollama("http://127.0.0.1:9", PAIR, msgs, 0.15, 4096, api_style="openai",
                      repeat_penalty=1.3, tools=False)
    except Exception as e:
        print(f"  (call_ollama raised {e!r})")
    try:
        m.call_openai_streaming("http://127.0.0.1:9", PAIR, msgs, 0.15, 4096,
                                repeat_penalty=1.3, tools=False)
    except Exception as e:
        print(f"  (call_openai_streaming raised {e!r})")
    return sent


def dbc_calls():
    m = _load(DBC, "dbc_pen")
    wires = []

    def fake_post(_base, wire, _timeout):
        wires.append(dict(wire))
        return {"choices": [{"message": {"content": '{"a": 1}'}, "finish_reason": "stop"}], "usage": {}}

    m._post = fake_post
    m.base_url = lambda: "http://fake"
    m.chat("http://fake", PAIR, "sys", "user", schema=None)
    return m, wires


def main():
    sent = worker_payloads()
    check("worker: both OpenAI-lane requests were sent", len(sent), 2)
    check("worker non-streaming openai body carries repetition_penalty",
          (sent[0] if sent else {}).get("repetition_penalty"), 1.3)
    check("worker streaming openai body carries repetition_penalty",
          (sent[1] if len(sent) > 1 else {}).get("repetition_penalty"), 1.3)
    check("worker keeps llama-server's repeat_penalty too",
          (sent[0] if sent else {}).get("repeat_penalty"), 1.3)
    m, wires = dbc_calls()
    check("darkbloom_chat sends the profile repetition_penalty (card: 1.0)",
          bool(wires) and wires[0].get("repetition_penalty") == 1.0, True)
    r = subprocess.run([sys.executable, str(DBC), "--self-test"], capture_output=True, text=True)
    check("darkbloom_chat self-test (retry raises the penalty)", "SELF_TEST_OK" in r.stdout, True)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("WORKER_SRC", "non-streaming: explicit repeat_penalty override dropped",
     '    _f = _profile_fields(model, role, api_style, temperature, num_ctx, top_p, top_k,\n                         max_tokens, repeat_penalty, think)',
     '    _f = _profile_fields(model, role, api_style, temperature, num_ctx, top_p, top_k,\n                         max_tokens, None, think)'),
    ("WORKER_SRC", "streaming: explicit repeat_penalty override dropped",
     '    _f = _profile_fields(model, role, "openai", temperature, num_ctx, top_p, top_k,\n                         max_tokens, repeat_penalty, think)',
     '    _f = _profile_fields(model, role, "openai", temperature, num_ctx, top_p, top_k,\n                         max_tokens, None, think)'),
    ("DBC_SRC", "darkbloom_chat retry not raised",
     '            wire["repetition_penalty"] = max(float(body.get("repetition_penalty") or 1.0),\n                                             RETRY_REPETITION_PENALTY)\n', ''),
]


def revert_check():
    bad = 0
    for env, name, old, new in MUTATIONS:
        src_path = WORKER if env == "WORKER_SRC" else DBC
        src = src_path.read_text()
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-" + src_path.name, delete=False,
                                         dir=str(src_path.parent)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, env: f.name},
                           capture_output=True, text=True, timeout=600)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
