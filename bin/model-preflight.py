#!/usr/bin/env python3
"""Pre-flight smoke test for an Ollama (or llama-server) model, run BEFORE
committing it to a real dispatch.

Built 2026-08-22 after a single session hit three independent bug classes
live, each costing a wasted ~30min dispatch before being caught by hand:
  1. Context-length mismatch: a model's real native context far exceeds
     whatever --num-ctx a caller was about to silently use (qwen3.8: real
     262144 vs an unexamined 32768).
  2. Chat template with zero tool-calling logic at all (devstral: Ollama's
     shipped template never references `tools` anywhere in the Jinja).
  3. Template looks correct but the serving backend's renderer still never
     emits tool_calls (deepseek-r1 distills against Ollama specifically --
     confirmed upstream, github.com/ollama/ollama/issues/8517).

Each bug class needs a DIFFERENT check -- a static template scan alone
would miss #3, and a live call alone is slow/expensive to be the first
thing you try. Steps below are ordered cheap-and-static first.

See Claude/Ollama-Dispatch-Log.md, "Model-onboarding smoke test" for the
full research this implements, and "Dispatch work queue" for how this is
meant to gate a future queue (distinct exit codes below, not just 0/1).
"""
import argparse
import importlib.util
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import gguf
except ImportError:
    gguf = None

try:
    import jinja2
except ImportError:
    jinja2 = None

# ollama-worker.py is a script, not a package (hyphenated filename), and
# already has everything needed here -- the tag-agnostic manual-tool-call
# parser, the OpenAI/Ollama-normalizing chat call, and the real dispatch
# loop for the round-trip test. Load it by path rather than duplicate any
# of that logic.
_WORKER_PATH = Path.home() / "bin" / "ollama-worker.py"
_spec = importlib.util.spec_from_file_location("ollama_worker", _WORKER_PATH)
ollama_worker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ollama_worker)


class Result:
    def __init__(self, name: str, status: str, detail: str):
        self.name = name
        self.status = status  # "PASS" | "WARN" | "FAIL" | "SKIP"
        self.detail = detail


def get_gguf_path(model: str) -> Path:
    result = subprocess.run(["ollama", "show", model, "--modelfile"],
                             capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"ollama show --modelfile failed: {result.stderr.strip()}")
    for line in result.stdout.splitlines():
        if line.startswith("FROM "):
            p = Path(line[len("FROM "):].strip())
            if not p.exists():
                raise RuntimeError(f"GGUF blob path from Modelfile does not exist: {p}")
            return p
    raise RuntimeError(f"no FROM line found in `ollama show --modelfile {model}` output")


def check_context(gguf_path: Path, intended_num_ctx: int) -> Result:
    if gguf is None:
        return Result("context-length", "SKIP", "gguf package not installed")
    reader = gguf.GGUFReader(str(gguf_path))
    arch_field = reader.get_field("general.architecture")
    if arch_field is None:
        return Result("context-length", "FAIL", "no general.architecture field in GGUF metadata")
    arch = arch_field.contents()
    ctx_field = reader.get_field(f"{arch}.context_length")
    if ctx_field is None:
        return Result("context-length", "FAIL", f"no {arch}.context_length field in GGUF (arch={arch})")
    real_ctx = int(ctx_field.contents())
    if intended_num_ctx > real_ctx:
        return Result("context-length", "FAIL",
                       f"intended --num-ctx {intended_num_ctx} EXCEEDS real context {real_ctx} "
                       f"(arch={arch}) -- would silently truncate or error")
    pct = 100 * intended_num_ctx / real_ctx
    if pct < 25:
        return Result("context-length", "WARN",
                       f"arch={arch}, real context={real_ctx}, intended={intended_num_ctx} "
                       f"({pct:.0f}% of real ceiling) -- confirm this is deliberate, not an "
                       f"unexamined default")
    return Result("context-length", "PASS",
                   f"arch={arch}, real context={real_ctx}, intended={intended_num_ctx} "
                   f"({pct:.0f}% of real ceiling)")


def check_template_tool_support(gguf_path: Path) -> Result:
    if gguf is None or jinja2 is None:
        return Result("tool-template", "SKIP", "gguf or jinja2 package not installed")
    reader = gguf.GGUFReader(str(gguf_path))
    tmpl_field = reader.get_field("tokenizer.chat_template")
    if tmpl_field is None:
        return Result("tool-template", "FAIL", "no tokenizer.chat_template field in GGUF at all")
    template_str = tmpl_field.contents()

    dummy_messages = [{"role": "user", "content": "hello"}]
    dummy_tools = [{
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        },
    }]

    env = jinja2.Environment(trim_blocks=True, lstrip_blocks=True)
    # Chat templates commonly call these globals; stub them so a render
    # fails only for reasons related to what we're actually testing.
    env.globals["strftime_now"] = lambda fmt: "2026-01-01"
    env.globals["raise_exception"] = lambda msg: (_ for _ in ()).throw(jinja2.exceptions.TemplateRuntimeError(msg))

    try:
        tmpl = env.from_string(template_str)
    except Exception as e:
        return Result("tool-template", "FAIL", f"template failed to parse at all: {e}")

    render_kwargs = dict(messages=dummy_messages, add_generation_prompt=True, bos_token="", eos_token="")
    try:
        without_tools = tmpl.render(tools=None, **render_kwargs)
    except Exception as e:
        without_tools = f"<<RENDER ERROR: {e}>>"
    try:
        with_tools = tmpl.render(tools=dummy_tools, **render_kwargs)
    except Exception as e:
        with_tools = f"<<RENDER ERROR: {e}>>"

    if without_tools == with_tools:
        return Result("tool-template", "FAIL",
                       "output IDENTICAL with and without a tools list -- template never "
                       "references `tools`, no tool-calling support at the template level")
    return Result("tool-template", "PASS",
                   f"output changes when tools are provided (without={len(without_tools)} chars, "
                   f"with={len(with_tools)} chars) -- template does branch on tools")


def check_live_tool_call(host: str, model: str, api_style: str) -> Result:
    try:
        resp = ollama_worker.call_ollama(
            host, model,
            [{"role": "user", "content": "Read the file called test.txt using the read_file tool."}],
            temperature=0.2, num_ctx=4096, timeout=120, tools=True, api_style=api_style,
        )
    except Exception as e:
        return Result("live-tool-call", "FAIL", f"request failed: {e}")
    msg = resp.get("message", {})
    tool_calls = msg.get("tool_calls") or []
    content = (msg.get("content") or "").strip()
    if tool_calls:
        names = [tc.get("function", {}).get("name") for tc in tool_calls]
        return Result("live-tool-call", "PASS", f"native tool_calls populated: {names}")
    manual_calls = ollama_worker.extract_manual_tool_calls(content) if content else []
    if manual_calls:
        names = [c.get("name") for c in manual_calls]
        return Result("live-tool-call", "WARN",
                       f"native tool_calls EMPTY, but a manually-parseable call was found in "
                       f"plain text ({names}) -- dispatch this model with --manual-tools")
    return Result("live-tool-call", "FAIL",
                   f"no tool call at all, native or manual. Model responded: {content[:200]!r}")


def check_marker_roundtrip(host: str, model: str, api_style: str, manual_tools: bool, tmp_dir: Path) -> Result:
    marker = secrets.token_hex(8)
    (tmp_dir / "marker_in.txt").write_text(marker)
    out_file = tmp_dir / "marker_out.txt"
    if out_file.exists():
        out_file.unlink()

    task = ("Read the file 'marker_in.txt' using the read_file tool, then write its exact "
            "contents (and nothing else) to a new file called 'marker_out.txt' using the "
            "write_file tool. Do this now.")
    try:
        exit_code = ollama_worker.run_task(
            model=model, host=host, cwd=str(tmp_dir), task=task, verify=None,
            max_iters=4, temperature=0.2, num_ctx=8192, searxng_host="http://unused:0",
            manual_tools=manual_tools, api_style=api_style,
        )
    except Exception as e:
        return Result("marker-roundtrip", "FAIL", f"dispatch raised: {e}")

    if not out_file.exists():
        return Result("marker-roundtrip", "FAIL", f"marker_out.txt was never written (worker exit={exit_code})")
    written = out_file.read_text().strip()
    if written == marker:
        return Result("marker-roundtrip", "PASS", f"marker round-tripped correctly (worker exit={exit_code})")
    return Result("marker-roundtrip", "FAIL", f"content mismatch: wrote {written!r}, expected {marker!r}")


def check_sampling_params(model: str) -> Result:
    result = subprocess.run(["ollama", "show", model, "--modelfile"],
                             capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return Result("sampling-params", "SKIP", f"ollama show failed: {result.stderr.strip()}")
    params = [ln.strip() for ln in result.stdout.splitlines() if ln.strip().startswith("PARAMETER")]
    if not params:
        return Result("sampling-params", "WARN",
                       "no PARAMETER lines in this Ollama package's Modelfile -- it doesn't carry "
                       "the upstream model's recommended sampling. Look up the real recommended "
                       "temperature/top_p/top_k by hand before dispatching; do not assume Ollama's "
                       "bare defaults are correct for this model.")
    return Result("sampling-params", "PASS", "; ".join(params))


def main():
    ap = argparse.ArgumentParser(description="Pre-flight smoke test for a model before a real dispatch.")
    ap.add_argument("--model", required=True)
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--api", choices=["ollama", "openai"], default="ollama")
    ap.add_argument("--intended-num-ctx", type=int, required=True,
                     help="The --num-ctx you actually plan to dispatch with -- checked against "
                          "the model's real context length from its own GGUF metadata.")
    ap.add_argument("--skip-live", action="store_true",
                     help="Static checks only (context length, template tool-support scan, "
                          "Modelfile sampling params) -- skip the two checks that make real API "
                          "calls (live tool-call probe, marker round-trip dispatch).")
    args = ap.parse_args()

    print(f"=== model-preflight: {args.model} @ {args.host} ({args.api}) ===")
    results = []

    gguf_path = None
    try:
        gguf_path = get_gguf_path(args.model)
        print(f"GGUF blob: {gguf_path}")
    except Exception as e:
        results.append(Result("gguf-resolve", "FAIL", str(e)))
        print(f"[FAIL ] gguf-resolve: {e}")

    if gguf_path:
        for r in (check_context(gguf_path, args.intended_num_ctx),
                  check_template_tool_support(gguf_path)):
            results.append(r)
            print(f"[{r.status:5s}] {r.name}: {r.detail}")

    manual_tools_needed = False
    if not args.skip_live:
        r3 = check_live_tool_call(args.host, args.model, args.api)
        results.append(r3)
        print(f"[{r3.status:5s}] {r3.name}: {r3.detail}")
        manual_tools_needed = (r3.status == "WARN")

        with tempfile.TemporaryDirectory(prefix="model-preflight-") as td:
            r4 = check_marker_roundtrip(args.host, args.model, args.api, manual_tools_needed, Path(td))
        results.append(r4)
        print(f"[{r4.status:5s}] {r4.name}: {r4.detail}")

    r5 = check_sampling_params(args.model)
    results.append(r5)
    print(f"[{r5.status:5s}] {r5.name}: {r5.detail}")

    fails = [r for r in results if r.status == "FAIL"]
    warns = [r for r in results if r.status == "WARN"]
    print("=== summary ===")
    for r in results:
        print(f"  {r.status:5s} {r.name}")
    if fails:
        print(f"RESULT: FAIL ({len(fails)} hard failure(s), {len(warns)} warning(s))")
        sys.exit(1)
    if warns:
        print(f"RESULT: PASS WITH WARNINGS ({len(warns)} warning(s)) -- review before dispatching")
        sys.exit(2)
    print("RESULT: CLEAN PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()
