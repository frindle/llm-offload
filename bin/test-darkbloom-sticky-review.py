#!/usr/bin/env python3
"""F1 regression: darkbloom_chat sticky local.json + code-review-agent protocol choice.
Darkbloom rewrites local.json on restart (briefly empty/torn); the review path must not
flip to Ollama /api/chat, lose the Bearer key, or fall back to the retired localhost:11434.
Run: python3 test-darkbloom-sticky-review.py
Revert-check: DBCHAT=<old darkbloom_chat.py> REVIEW=<old code-review-agent.py> python3 this.py -> FAIL
(or --revert-check: pulls the pre-fix copies from machine-config git HEAD and requires FAIL)."""
import os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
DBCHAT = Path(os.environ.get("DBCHAT", HERE / "darkbloom_chat.py"))
REVIEW = Path(os.environ.get("REVIEW", HERE / "code-review-agent.py"))
GOOD = '{"api_key":"dk-test-1","base_url":"http://127.0.0.1:8000/v1","port":8000}'
fails = []


def chk(name, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f" (got {got!r}, want {want!r})"))
    if not ok:
        fails.append(name)


def load():
    os.environ.pop("DARKBLOOM_STICKY_S", None); os.environ.pop("DARKBLOOM_REVIEW_GUARD", None)
    sys.modules.pop("darkbloom_chat", None)
    d = SourceFileLoader("darkbloom_chat", str(DBCHAT)).load_module()
    sys.modules["darkbloom_chat"] = d
    return d


def main():
    d = load()
    tmp = Path(tempfile.mkdtemp(prefix="f1-"))
    lj = tmp / "local.json"
    d.LOCAL_JSON = str(lj)
    clock = [1000.0]
    d._now = lambda: clock[0]
    HOST = "http://127.0.0.1:8000"

    lj.write_text(GOOD)
    chk("healthy: base_url", d.base_url(), HOST)
    chk("healthy: is_darkbloom", d.is_darkbloom(HOST), True)
    chk("healthy: key", d._record().get("api_key"), "dk-test-1")
    for label, txt in (("empty", ""), ("torn", '{"api_key": "dk-te'), ("non-object", "[]")):
        lj.write_text(txt)
        chk(f"{label}: base_url sticky", d.base_url(), HOST)
        chk(f"{label}: is_darkbloom stays True", d.is_darkbloom(HOST), True)
        chk(f"{label}: Bearer key sticky", d._record().get("api_key"), "dk-test-1")
        chk(f"{label}: default_host is Darkbloom, not legacy", d.default_host(), HOST)
        chk(f"{label}: legacy host not misrouted-silently", bool(d.legacy_ollama_misroute("http://localhost:11434")), True)
    # expiry: sticky is bounded
    lj.write_text("")
    clock[0] += 61
    chk("expired: base_url None", d.base_url(), None)
    # kill switch
    lj.write_text(GOOD); d.base_url()
    lj.write_text("")
    os.environ["DARKBLOOM_STICKY_S"] = "0"
    chk("kill switch DARKBLOOM_STICKY_S=0: old behaviour", d.base_url(), None)
    os.environ.pop("DARKBLOOM_STICKY_S")
    # explicit off is never masked
    lj.write_text(GOOD); d.base_url()
    lj.unlink()
    chk("missing file: not masked", d.base_url(), None)
    chk("missing file: is_darkbloom False", d.is_darkbloom(HOST), False)
    lj.write_text(GOOD); d.base_url()
    lj.write_text('{"version":"0.9.20"}')
    chk("keyless record: not masked", d.base_url(), None)
    # fresh process inside the restart window (no last-good): fixed local endpoint is Darkbloom
    d._GOOD[0] = None
    lj.write_text("")
    chk("fresh+empty: is_darkbloom(DEFAULT_BASE)", d.is_darkbloom(HOST), True)
    chk("fresh+empty: default_host not legacy", d.default_host(), HOST)
    chk("fresh+empty: Ollama host 198.51.100.145 not Darkbloom", d.is_darkbloom("http://198.51.100.145:11434"), False)
    lj.unlink()
    chk("no darkbloom at all: legacy default allowed", d.default_host(), "http://localhost:11434")
    chk("no darkbloom at all: no misroute", d.legacy_ollama_misroute("http://localhost:11434"), None)
    # served_models sticky
    d._GOOD_SERVED[0] = frozenset(); 
    d.PROVIDER_TOML = str(tmp / "p.toml"); d.LOADED_MODELS_JSON = str(tmp / "l.json")
    Path(d.PROVIDER_TOML).write_text('enabled_models = ["Qwen3.5-9B"]\n')
    chk("served healthy", d.served_models(), {"qwen3.5-9b"})
    Path(d.PROVIDER_TOML).write_text("")
    chk("served empty read -> last good", d.served_models(), {"qwen3.5-9b"})
    chk("served injected args untouched by cache", d.served_models(toml_text="", loaded=[]), set())

    # ---- review path: Model.chat protocol choice
    sys.path.insert(0, str(HERE))
    r = SourceFileLoader("cra_f1", str(REVIEW)).load_module()
    r._dbk = d
    lj.write_text(GOOD); d.base_url()
    seen = {}
    d.chat = lambda host, *a, **k: (seen.setdefault("dbk", host), ("{}", "stop"))[1]
    def boom(*a, **k):
        seen["ollama"] = True
        raise AssertionError("ollama path used")
    r.urllib.request.urlopen = boom
    m = r.Model(HOST, "m", 4096)
    lj.write_text("")                     # mid-restart
    seen.clear()
    try:
        m.chat("s", "u")
    except Exception as e:
        seen["exc"] = repr(e)
    chk("review mid-restart: goes to darkbloom_chat", seen.get("dbk"), HOST)
    chk("review mid-restart: never Ollama /api/chat", "ollama" in seen, False)
    # legacy host while Darkbloom configured -> loud named failure, no Ollama call
    m2 = r.Model("http://localhost:11434", "m", 4096)
    seen.clear()
    try:
        m2.chat("s", "u")
    except RuntimeError as e:
        seen["exc"] = str(e)
    except Exception as e:
        seen["exc"] = "other:" + repr(e)
    chk("review legacy host: named reason", str(seen.get("exc", "")).startswith("darkbloom_misroute"), True)
    chk("review legacy host: no Ollama call", "ollama" in seen, False)
    # guard kill switch
    os.environ["DARKBLOOM_REVIEW_GUARD"] = "0"
    chk("guard kill switch", d.legacy_ollama_misroute("http://localhost:11434"), None)
    os.environ.pop("DARKBLOOM_REVIEW_GUARD")
    # real Ollama host untouched
    chk("Unraid host untouched", d.legacy_ollama_misroute("http://198.51.100.145:11434"), None)
    print("F1_STICKY_OK" if not fails else f"F1_STICKY_FAILED: {fails}")
    return 1 if fails else 0


def revert_check():
    top = subprocess.run(["git", "-C", str(HERE.parent / "Desktop/GitHub Projects/machine-config"), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True).stdout.strip()
    old = {}
    for n in ("darkbloom_chat.py", "code-review-agent.py"):
        p = Path(tempfile.mkdtemp(prefix="f1old-")) / n
        p.write_text(subprocess.run(["git", "-C", top, "show", f"HEAD:bin/{n}"], capture_output=True, text=True, check=True).stdout)
        old[n] = p
    env = dict(os.environ, DBCHAT=str(old["darkbloom_chat.py"]), REVIEW=str(old["code-review-agent.py"]))
    rc = subprocess.run([sys.executable, __file__], env=env).returncode
    print("REVERT_CHECK_OK (pre-fix code fails)" if rc != 0 else "REVERT_CHECK_FAILED (pre-fix code passed!)")
    return 0 if rc != 0 else 1


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
