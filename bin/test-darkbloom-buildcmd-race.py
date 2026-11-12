#!/usr/bin/env python3
"""_build_cmd must still speak --api openai to Darkbloom when local.json is momentarily
empty (psc-C10-a465e147cf00, 2026-10-13).

ROOT CAUSE: Darkbloom rewrites ~/.darkbloom/local.json on every provider restart. The lane
chooser picked http://127.0.0.1:8000 from a good read; _build_cmd re-read the (empty) file,
decided the host was not Darkbloom, and launched the worker with no --api openai. The
worker then refused to pull a Darkbloom-only model on 'Ollama' host :8000 (reason=crashed).

Usage: python3 ~/bin/test-darkbloom-buildcmd-race.py   (QUEUE=path to test a backup)
Prints DARKBLOOM_BUILDCMD_OK."""
import os, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUEUE = Path(os.environ.get("QUEUE", HERE / "ollama-queue.py"))
os.environ.pop("DARKBLOOM_BASE_URL", None)
q = SourceFileLoader("q_buildcmd_race", str(QUEUE)).load_module()
fails = 0


def chk(name, cond, detail=""):
    global fails
    print(("ok   - " if cond else "FAIL - ") + name + ("" if cond else f": {detail!r}"))
    fails += 0 if cond else 1


tf = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
tf.write("task"); tf.close()
job = {"id": "x", "model": "qwen3.6-35b-a3b-vl-mtp-mxfp8", "cwd": "/tmp", "task_file": tf.name,
       "num_ctx": 8192, "host_pref": "auto", "api": "ollama", "verify": "true"}
HOST = "http://127.0.0.1:8000"


def api_of(cmd):
    return cmd[cmd.index("--api") + 1] if "--api" in cmd else None


q._darkbloom_record = lambda: {"api_key": "k", "base_url": HOST + "/v1"}
chk("good local.json: --api openai", api_of(q._build_cmd(dict(job), HOST)) == "openai")
q._darkbloom_record = lambda: {}                      # the restart window
c = q._build_cmd(dict(job), HOST)
chk("EMPTY local.json (race): still --api openai", api_of(c) == "openai", c)
chk("EMPTY local.json: host arg intact", c[c.index("--host") + 1] == HOST)
chk("a genuine Ollama host is NOT treated as Darkbloom", api_of(q._build_cmd(dict(job), "http://198.51.100.145:11434")) is None)
chk("empty record, trailing /v1 form recognised", q._is_darkbloom_host(HOST + "/v1"))
chk("None/empty host is not Darkbloom", not q._is_darkbloom_host("") and not q._is_darkbloom_host(None))
# adversarial review 2026-10-13: each of the two fallbacks must hold ON ITS OWN (above, the first good
# read set the last-good URL == the fixed default, so dropping either one still passed).
q.DARKBLOOM_STICKY_S = 0.0                            # isolate _is_darkbloom_host from the chooser's sticky window
q._DARKBLOOM_LAST_URL[0] = None                       # (a) fresh daemon, first build lands in the restart window
q._darkbloom_record = lambda: {}
chk("fresh process + EMPTY local.json: the fixed local endpoint alone still means Darkbloom",
    api_of(q._build_cmd(dict(job), q.DARKBLOOM_DEFAULT_BASE)) == "openai")
ALT = "http://127.0.0.1:8001"                         # (b) a non-default base_url seen once, then the file empties
q._darkbloom_record = lambda: {"api_key": "k", "base_url": ALT + "/v1"}
chk("non-default base_url, good read: --api openai", api_of(q._build_cmd(dict(job), ALT)) == "openai")
q._darkbloom_record = lambda: {}
chk("non-default base_url, then EMPTY local.json: the last good URL alone still means Darkbloom",
    api_of(q._build_cmd(dict(job), ALT)) == "openai")
chk("a port never reported by local.json is not Darkbloom", api_of(q._build_cmd(dict(job), "http://127.0.0.1:8002")) is None)
# a MIS-SET kill switch must not take the daemon down at import (DARKBLOOM_STICKY_S=off raised ValueError)
import subprocess
for _v, _want in (("off", "0.0"), ("", "60.0"), ("garbage", "60.0"), ("5", "5.0"), ("0", "0.0")):
    _p = subprocess.run([sys.executable, "-c",
                         "import sys\nfrom importlib.machinery import SourceFileLoader as L\n"
                         "q = L('q_sticky', sys.argv[1]).load_module()\nprint('STICKY', q.DARKBLOOM_STICKY_S)", str(QUEUE)],
                        env={**os.environ, "DARKBLOOM_STICKY_S": _v, "OLLAMA_QUEUE_NO_NOTIFY": "1"},
                        capture_output=True, text=True, timeout=120)
    chk(f"DARKBLOOM_STICKY_S={_v!r}: queue imports, window = {_want}", f"STICKY {_want}" in _p.stdout,
        (_p.stdout + _p.stderr)[-200:])
os.unlink(tf.name)
print("DARKBLOOM_BUILDCMD_OK" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
