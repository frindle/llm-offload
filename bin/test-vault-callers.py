#!/usr/bin/env python3
"""Tests for the vault callers' URL/token resolution + PUT-on-404 monthly roll, run against the
real vault-shim on loopback (temp VAULT_ROOT). Never touches the real vault/Unraid.

  test-vault-callers.py                # run the suite
  test-vault-callers.py --revert-tests # on temp copies: the unmutated control must PASS, every mutant must FAIL
Targets are read from $VC_BIN (default ~/bin), the shim from $VC_SHIM (default: the vault-stack checkout).
$VC_DEFAULT_URL is the URL the suite expects with no env file (the revert copies rewrite the real one).
"""
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SHIM = Path(os.environ.get("VC_SHIM", "/Users/user/Desktop/GitHub Projects/vault-stack/shim/vault_shim.py"))
BIN = Path(os.environ.get("VC_BIN", str(Path.home() / "bin")))
SELF = Path(__file__).resolve()
REAL_URL = "http://192.0.2.240:27123"
UNREACHABLE = "http://127.0.0.1:9"      # revert copies must never reach the real vault
DEFAULT_URL = os.environ.get("VC_DEFAULT_URL", REAL_URL)
TOKEN = "test-token-NOT-REAL-1234567890"
DEFAULT_FILE_TOKEN = "default-file-token"
TARGETS = ("ollama-worker.py", "vault-semantic.py", "session-summary-index.py", "vault_conf.py", "vault-log-append",
           "sanitize_apidocs.py")


def main():
    if "--revert-tests" in sys.argv:
        return revert_tests()
    return 0 if suite() else 1


def default_token_rel() -> Path:
    """vault_conf's default token file, relative to HOME (vault_conf.py holds its one definition)."""
    spec = importlib.util.spec_from_file_location("vc_probe", BIN / "vault_conf.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.DEFAULT_TOKEN_FILE.relative_to(Path.home())


def suite() -> bool:
    ok = True
    n = {"pass": 0}

    def check(name, got, want=True):
        nonlocal ok
        if got == want:
            n["pass"] += 1
            print(f"PASS {name}")
        else:
            ok = False
            print(f"FAIL {name}: got {got!r} want {want!r}")

    rel_tok = default_token_rel()
    td = Path(tempfile.mkdtemp(prefix="vault-callers-test-"))
    home = td / "home"
    (home / ".config" / "vault").mkdir(parents=True)
    root = td / "root"; (root / "Claude").mkdir(parents=True)
    (td / "state").mkdir()
    tokf = td / "shim-token"; tokf.write_text(TOKEN + "\n")
    # the default token file holds a DIFFERENT value: proves the env file's token file wins
    (home / rel_tok).parent.mkdir(parents=True, exist_ok=True)
    (home / rel_tok).write_text(DEFAULT_FILE_TOKEN + "\n")
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    url = f"http://127.0.0.1:{port}"
    (home / ".config" / "vault" / "env").write_text(f"VAULT_URL={url}\nVAULT_TOKEN_FILE={tokf}\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("VAULT_")}
    env.update(HOME=str(home))
    shim = subprocess.Popen(
        [sys.executable, str(SHIM)], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        env={**env, "VAULT_ROOT": str(root), "VAULT_TOKEN_FILE": str(tokf), "SHIM_BIND": "127.0.0.1",
             "SHIM_PORT": str(port), "STATE_DIR": str(td / "state")})
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(url + "/healthz", timeout=1).read(); break
            except Exception:
                time.sleep(0.1)
        else:
            print("FAIL shim did not start"); return False

        month = f"{datetime.now(timezone.utc):%Y-%m}"
        logp = root / "Claude" / f"Agent-Dispatch-Log-{month}.md"

        # ---- 1. worker: env-file URL+token, PUT-on-404 roll, then append ----
        os.environ.clear(); os.environ.update(env)
        sys.path.insert(0, str(BIN))
        sys.modules.pop("vault_conf", None)
        spec = importlib.util.spec_from_file_location("ow_under_test", BIN / "ollama-worker.py")
        ow = importlib.util.module_from_spec(spec); spec.loader.exec_module(ow)
        ow.log = lambda *a, **k: None
        check("worker caches no token in a module global at import",
              any(v in (TOKEN, DEFAULT_FILE_TOKEN) for v in vars(ow).values() if isinstance(v, str)), False)
        u, t = ow._vault_endpoint()
        check("worker resolves URL from ~/.config/vault/env", u, url)
        check("worker resolves token from the env file's token file (not the default file)", t, TOKEN)
        ow.log_dispatch_to_vault("m1", "task one", True, True, Path("/tmp/x.json"), dispatch_tag="j1")
        check("worker: missing monthly note is CREATED via PUT-on-404 roll", logp.exists())
        body = logp.read_text() if logp.exists() else ""
        check("rolled note has header + first entry", ("# Agent Dispatch Log" in body) and ("id=`j1`" in body))
        ow.log_dispatch_to_vault("m2", "task two", False, False, Path("/tmp/y.json"), dispatch_tag="j2")
        body2 = logp.read_text() if logp.exists() else ""
        check("second dispatch APPENDS (entry 1 kept, entry 2 added)", ("id=`j1`" in body2) and ("id=`j2`" in body2))
        check("append did not re-write the header", body2.count("# Agent Dispatch Log"), 1)
        # non-fatal on failure: dead URL + env var override must not raise
        os.environ["VAULT_URL"] = "http://127.0.0.1:1"
        try:
            ow.log_dispatch_to_vault("m3", "t", True, True, Path("/tmp/z"))
            check("worker logging failure is non-fatal", True)
        except Exception as e:
            check("worker logging failure is non-fatal", repr(e), "no raise")
        del os.environ["VAULT_URL"]
        # no env file -> the default URL and the default token file (no network here)
        (home / ".config" / "vault" / "env").rename(td / "env.off")
        u2, t2 = ow._vault_endpoint()
        check("no env file -> default URL", u2, DEFAULT_URL)
        check("no env file -> default token file", t2, DEFAULT_FILE_TOKEN)
        (td / "env.off").rename(home / ".config" / "vault" / "env")

        # ---- 2. vault-log-append: POST append through the shim ----
        (root / "Claude" / "VLA-Test.md").write_text("# VLA\n")
        r = subprocess.run([sys.executable, str(BIN / "vault-log-append"), "--base-note", "Claude/VLA-Test", "-"],
                           env=env, input="appended via shim\n", capture_output=True, text=True, timeout=60, cwd=td)
        check("vault-log-append exits 0 against the shim", r.returncode, 0)
        check("vault-log-append appended the text", "appended via shim" in (root / "Claude" / "VLA-Test.md").read_text())
        check("vault-log-append never prints the token", TOKEN in (r.stdout + r.stderr), False)

        # ---- 3. vault-semantic list/read through the shim ----
        (root / "Claude" / "n.md").write_text("hello " * 20)
        r = subprocess.run([sys.executable, "-c",
            "import importlib.util,sys;sys.path.insert(0,sys.argv[1]);"
            "s=importlib.util.spec_from_file_location('vs',sys.argv[1]+'/vault-semantic.py');m=importlib.util.module_from_spec(s);"
            "s.loader.exec_module(m);print(m.VAULT_URL);print(m.list_notes('Claude'))", str(BIN)],
            env=env, capture_output=True, text=True, timeout=60, cwd=td)
        check("vault-semantic uses env-file URL", url in r.stdout)
        check("vault-semantic lists notes through the shim w/ token", "Claude/n.md" in r.stdout)

        # ---- 4. session-summary-index: shim_put, and --write with no VAULT_SESSIONINDEX goes through the shim ----
        r = subprocess.run([sys.executable, "-c",
            "import importlib.util,sys;s=importlib.util.spec_from_file_location('ss',sys.argv[1]+'/session-summary-index.py');"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);m.shim_put('t-1.md','---\\nx: 1\\n---\\nbody')", str(BIN)],
            env=env, capture_output=True, text=True, timeout=60, cwd=td)
        check("session-summary-index shim_put succeeds", r.returncode, 0)
        check("...and the note landed in Claude/SessionIndex", (root / "Claude" / "SessionIndex" / "t-1.md").exists())
        proj = td / "proj"; proj.mkdir()
        sid = "abcd1234-0000-4000-8000-000000000000"
        (proj / f"{sid}.jsonl").write_text(json.dumps({
            "type": "user", "sessionId": sid, "timestamp": "2026-11-06T10:00:00Z", "cwd": "/x/proj",
            "message": {"role": "user", "content": "hello index"}}) + "\n")
        r = subprocess.run([sys.executable, str(BIN / "session-summary-index.py"), "--write", "--session", "abcd1234",
                            "--project-dir", str(proj)], env=env, capture_output=True, text=True, timeout=60, cwd=td)
        check("session-summary-index --write (VAULT_SESSIONINDEX unset) exits 0", r.returncode, 0)
        check("...and PUT the session note through the shim",
              len(list((root / "Claude" / "SessionIndex").glob("*abcd1234*.md"))), 1)
        check("...and wrote nothing under ~/Documents", (home / "Documents").exists(), False)

        # ---- 5. sanitize_apidocs: list + read Claude/API-Docs through the shim (dry run) ----
        (root / "Claude" / "API-Docs").mkdir()
        (root / "Claude" / "API-Docs" / "Demo.md").write_text("# Demo API\n\nGET /items returns a list.\n")
        repo = td / "apidocs-repo"; repo.mkdir()
        r = subprocess.run([sys.executable, str(BIN / "sanitize_apidocs.py"), str(repo), "--dry"],
                           env=env, capture_output=True, text=True, timeout=60, cwd=td)
        check("sanitize_apidocs --dry exits 0 against the shim", r.returncode, 0)
        check("...listed and read the Demo note", ("Demo" in r.stdout) and ("dry run, gate passed" in r.stdout))
        check("...and wrote nothing to the repo", any(repo.iterdir()), False)
    finally:
        shim.terminate(); shim.wait(timeout=10)
        shutil.rmtree(td, ignore_errors=True)
    print(f"{n['pass']} passed")
    print("SUITE_OK" if ok else "SUITE_FAILED")
    return ok


def revert_tests() -> int:
    """Run the suite on temp copies of the targets, with the real vault address rewritten to an unreachable
    port: the unmutated control copy must PASS (else the mutant results mean nothing), every mutant must FAIL."""
    muts = [
        ("control (no mutation)", None, None, None),
        ("worker ignores vault_conf for the URL", "ollama-worker.py",
         "vault_conf.vault_url(VAULT_URL_DEFAULT)", "VAULT_URL_DEFAULT"),
        ("worker: PUT-on-404 roll removed", "ollama-worker.py", "if he.code == 404:", "if False:"),
        ("worker: append becomes PUT (clobbers log)", "ollama-worker.py",
         'data=entry.encode(), method="POST"', 'data=entry.encode(), method="PUT"'),
        ("worker: token read at import again", "ollama-worker.py",
         'VAULT_DISPATCH_LOG_BASE = "Claude/Agent-Dispatch-Log"',
         'VAULT_DISPATCH_LOG_BASE = "Claude/Agent-Dispatch-Log"\n_TOK = __import__("vault_conf").vault_token()'),
        ("vault_conf: env file ignored for URL", "vault_conf.py",
         'or read_env_file(conf_path).get("VAULT_URL")', ""),
        ("vault_conf: token file from env file ignored", "vault_conf.py",
         'or read_env_file(conf_path).get("VAULT_TOKEN_FILE")', ""),
        ("vault-semantic: URL hardwired", "vault-semantic.py",
         f'vault_conf.vault_url("{REAL_URL}")', f'"{UNREACHABLE}"'),
        ("session-summary-index: URL hardwired", "session-summary-index.py",
         "{vault_conf.vault_url()}", UNREACHABLE),
        ("session-summary-index: --write with VAULT_SESSIONINDEX unset skips the shim", "session-summary-index.py",
         "if args.write and (args.via_shim or not VAULT_SESSIONINDEX):", "if args.write and args.via_shim:"),
        ("vault-log-append: URL hardwired", "vault-log-append",
         "url = vault_conf.vault_url()", f'url = "{UNREACHABLE}"'),
        ("vault-log-append: token not from vault_conf", "vault-log-append",
         "token = vault_conf.vault_token()", 'token = "not-the-token"'),
        ("sanitize_apidocs: URL hardwired", "sanitize_apidocs.py",
         'vault_conf.vault_url() + "/vault/Claude/API-Docs/"', f'"{UNREACHABLE}/vault/Claude/API-Docs/"'),
        ("sanitize_apidocs: token not from vault_conf", "sanitize_apidocs.py",
         "TOK = vault_conf.vault_token()", 'TOK = "not-the-token"'),
    ]
    bad = 0
    for name, fn, old, new in muts:
        td = Path(tempfile.mkdtemp(prefix="vault-callers-mut-"))
        try:
            for f in TARGETS:  # no copy may ever address the real vault
                (td / f).write_text((BIN / f).read_text().replace(REAL_URL, UNREACHABLE)
                                    .replace("192.0.2.240:27123", "127.0.0.1:9"))
            if fn is not None:
                p = td / fn
                t = p.read_text()
                pat = old.replace(REAL_URL, UNREACHABLE)
                if t.count(pat) != 1:
                    print(f"BROKEN-MUTANT (pattern found {t.count(pat)}x, want 1) {name}"); bad += 1; continue
                p.write_text(t.replace(pat, new))
            r = subprocess.run([sys.executable, str(SELF)],
                               env={**os.environ, "VC_BIN": str(td), "VC_DEFAULT_URL": UNREACHABLE},
                               capture_output=True, text=True, timeout=300)
            if fn is None:
                if r.returncode == 0:
                    print(f"CONTROL-OK (suite passes) : {name}")
                else:
                    print(f"CONTROL-FAILED (suite fails on the unmutated copy) : {name}"); bad += 1
                    print("\n".join(ln for ln in r.stdout.splitlines() if ln.startswith("FAIL")))
            elif r.returncode != 0:
                print(f"REVERT-OK (suite fails) : {name}")
            else:
                print(f"REVERT-MISSED (suite still passes) : {name}"); bad += 1
        finally:
            shutil.rmtree(td, ignore_errors=True)
    print("REVERT_TESTS_OK" if not bad else f"REVERT_TESTS_FAILED ({bad})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
