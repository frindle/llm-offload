#!/usr/bin/env python3
"""Shared vault endpoint/token resolution for the operator's vault callers (stdlib only).

The vault is Basic Memory behind vault-shim, which serves a small REST API on :27123 with a
bearer token, so callers only need a base URL and a token source.
Resolution order, per key:
  VAULT_URL         env VAULT_URL -> ~/.config/vault/env -> caller default (DEFAULT_URL)
  VAULT_TOKEN_FILE  env VAULT_TOKEN_FILE -> ~/.config/vault/env -> DEFAULT_TOKEN_FILE
  token             contents of the token file ('Bearer ' prefix stripped)
~/.config/vault/env is KEY=VALUE lines (# comments ok). The token is returned to the caller only;
this module never logs it; the only CLI mode that emits it is `auth` (stdout, for capture).

  vault_conf.py url [DEFAULT]       -> prints the resolved base URL
  vault_conf.py auth                -> prints 'Bearer <token>' on stdout (capture it; never pass it in argv)
  vault_conf.py --self-test
"""
import os
import os as _qc_os, sys as _qc_sys
_qc_dir = _qc_os.path.dirname(_qc_os.path.realpath(__file__))
if _qc_dir not in _qc_sys.path:
    _qc_sys.path.append(_qc_dir)
import queue_config
import sys
from pathlib import Path

CONF_PATH = Path.home() / ".config" / "vault" / "env"
# The live shim token's file. Its name predates the shim; renaming it means moving every reader
# (other repos and hosts included) in the same change, so it keeps the old name until then.
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "ollama-worker" / "obsidian-token"
DEFAULT_URL = queue_config.get_str("vault.url", "", env="VAULT_URL")


def read_env_file(path=None) -> dict:
    out = {}
    try:
        for ln in Path(path or CONF_PATH).read_text().splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, _, v = ln.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def vault_url(default=DEFAULT_URL, env=None, conf_path=None) -> str:
    e = os.environ if env is None else env
    url = e.get("VAULT_URL") or read_env_file(conf_path).get("VAULT_URL") or default
    return url.rstrip("/")


def token_file(env=None, conf_path=None) -> Path:
    e = os.environ if env is None else env
    p = e.get("VAULT_TOKEN_FILE") or read_env_file(conf_path).get("VAULT_TOKEN_FILE")
    return Path(os.path.expanduser(p)) if p else DEFAULT_TOKEN_FILE


def vault_token(env=None, conf_path=None) -> str:
    """Raw bearer token (no 'Bearer ' prefix), or '' if unavailable."""
    try:
        tok = token_file(env, conf_path).read_text().strip()
    except OSError:
        tok = ""
    return tok[7:].strip() if tok.lower().startswith("bearer ") else tok


def _self_test() -> bool:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        if got == want:
            print(f"PASS {name}")
        else:
            ok = False
            print(f"FAIL {name}: got {got!r} want {want!r}")

    td = Path(tempfile.mkdtemp(prefix="vault-conf-selftest-"))
    conf = td / "env"
    tf = td / "tok"
    tf.write_text("Bearer s3cret\n")
    tf2 = td / "tok2"
    tf2.write_text("other\n")
    check("no env/file -> caller default", vault_url("http://x:1", {}, td / "none"), "http://x:1")
    check("no env/file -> default token file", token_file({}, td / "none"), DEFAULT_TOKEN_FILE)
    conf.write_text(f"# c\nVAULT_URL=http://shim:27123/\nVAULT_TOKEN_FILE={tf}\n")
    check("env file URL used (trailing slash stripped)", vault_url("http://x:1", {}, conf), "http://shim:27123")
    check("env var beats env file", vault_url("d", {"VAULT_URL": "http://e:2"}, conf), "http://e:2")
    check("env file token file used", token_file({}, conf), tf)
    check("token read from file, 'Bearer ' prefix stripped", vault_token({}, conf), "s3cret")
    check("env VAULT_TOKEN_FILE beats the env file's", vault_token({"VAULT_TOKEN_FILE": str(tf2)}, conf), "other")
    check("missing token file -> empty, no raise", vault_token({"VAULT_TOKEN_FILE": str(td / "nope")}, td / "none"), "")
    import shutil
    shutil.rmtree(td, ignore_errors=True)
    print("SELF_TEST_OK" if ok else "SELF_TEST_FAILED")
    return ok


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["--self-test"]:
        sys.exit(0 if _self_test() else 1)
    if a[:1] == ["url"]:
        print(vault_url(a[1] if len(a) > 1 else DEFAULT_URL))
    elif a[:1] == ["auth"]:
        t = vault_token()
        if not t:
            sys.exit(1)
        print(f"Bearer {t}")
    else:
        sys.exit(__doc__)
