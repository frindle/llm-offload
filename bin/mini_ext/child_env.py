"""child_env -- a fixed allowlist of environment variables for the commands the worker's model runs.

The agent's shell and the test runs it starts inherit mini's environment. Tokens and keys in it would be
readable by any command the model writes (`env`, `printenv`, a test that prints os.environ). The launcher
(the runner's LAUNCH stub) calls install() when MINI_CHILD_ENV=minimal, and from then on every command
mini's local environment executes gets only the names below.

What the commands need, checked against the lane: PATH and HOME (every tool), locale and TERM, a temp dir,
the interpreter and toolchain locations (a venv, PYTHONPATH, NODE_PATH, the CA bundle for installers that
reach a mirror), mini's own pager/progress settings from mini.yaml, and DISPATCH_VERIFY_SANDBOX (which makes
the queue CLI refuse mutating subcommands from inside a run). Prefixes LC_ and ANCHOR_EDIT_ are allowed.
"""
import os

ALLOW = frozenset((
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "TMPDIR", "TZ", "PWD",
    "VIRTUAL_ENV", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONHASHSEED", "PYTHONUTF8", "NODE_PATH",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CI",
    "PAGER", "MANPAGER", "LESS", "PIP_PROGRESS_BAR", "TQDM_DISABLE",
    "DISPATCH_VERIFY_SANDBOX",
))
ALLOW_PREFIX = ("LC_", "ANCHOR_EDIT_")


def minimal_env(env=None):
    """A copy of env (default os.environ) holding only allowlisted names."""
    env = os.environ if env is None else env
    return {k: v for k, v in env.items() if k in ALLOW or k.startswith(ALLOW_PREFIX)}


def install():
    """Wrap minisweagent's local command runner so every command it executes gets minimal_env(). Returns True when
    wrapped; False (and nothing changed) when the module has no `_run` to wrap, so a mini upgrade never breaks a run."""
    try:
        from minisweagent.environments import local
    except ImportError:
        return False
    run = getattr(local, "_run", None)
    if run is None:
        return False

    def scrubbed(command, cwd, env, timeout):
        return run(command, cwd, minimal_env(env), timeout)

    local._run = scrubbed
    return True
