#!/usr/bin/env python3
"""Queue runner wrapper for the v10-bulk bake-off (the owner 2026-09-09: ALL GPU work,
bake-offs included, must run THROUGH ollama-queue -- no standalone nohup).

The queue's --runner contract calls: `python3 <runner> --model --host --num-ctx
--cwd --task-file`. The bake-off is a bash driver that manages its OWN models,
hosts, contexts and scoring internally, so this wrapper accepts (and ignores) the
queue's per-model flags and simply execs the driver as ONE long Studio job. While
it runs, the queue treats it as occupying the Studio lane, which is exactly the
serialization we want -- nothing else on the GPU concurrently.

Resume is the DRIVER's job: bakeoff-v10-bulk-macstudio.sh skips any (model,rep)
cell already recorded in its results CSV, so a re-queue resumes rather than
restarting from rep 1.

ROSTER_ONLY is pinned here to the 6-model new-cohort scope the killed nohup was
running, so the re-queue continues the SAME run, not the full 17-model roster.
Override with $BAKEOFF_ROSTER_ONLY if a future enqueue wants a different scope.
"""
import argparse, json, os, re, signal, subprocess, sys
from pathlib import Path

DRIVER = "/Users/user/Desktop/GitHub Projects/bakeoff/bakeoff-v10-bulk-macstudio.sh"

# --- UNRAID OVERFLOW LANE (2026-10-01) ---------------------------------------
# Bake-offs are NON-PRODUCTION candidate-model evals: nothing downstream depends
# on their output, and they are the single biggest consumer of GPU-hours in the
# pipeline. With all real dispatch now on local Darkbloom, a bake-off on the
# Studio lane directly delays production work -- so candidate evals that can run
# on the 3080 should overflow to Unraid instead.
#
# The queue pins the lane (`enqueue --host unraid`) and passes it here as --host.
# This wrapper's job is to make that pin REAL rather than decorative: the v10
# drivers hardcode `HOST="http://localhost:11434"` internally, so a job the queue
# believes is running on Unraid would quietly put the whole eval on the Mac's GPU
# -- the exact opposite of the intent, and invisible in the queue view.
#
# So: resolve the lane, export it for the driver, and ABORT on a provable
# mismatch between the lane the queue pinned and the host the driver will
# actually hit. Fail loudly rather than mis-measure on the wrong GPU.
HOSTS_JSON = Path(os.environ.get("OLLAMA_DISPATCH_HOSTS")
                  or (Path.home() / ".ollama-dispatch" / "hosts.json"))
LOCAL_URLS = ("http://localhost:11434", "http://127.0.0.1:11434")
# Escape hatch for a driver that genuinely manages its own hosts per cell (the
# older multi-host drivers do). Off by default: silence is how a bake-off gets
# measured on the wrong card.
ALLOW_HOST_MISMATCH = os.environ.get("BAKEOFF_ALLOW_HOST_MISMATCH") == "1"


def resolve_host(host: str) -> str:
    """The base URL for a queue --host value, which may be a lane name ('unraid',
    'studio', 'studio-db') or already a URL. PURE apart from reading hosts.json."""
    h = str(host or "").strip()
    if not h:
        return ""
    if h.startswith("http://") or h.startswith("https://"):
        return h.rstrip("/")
    if h.lower() in ("local", "studio", "studio-db", "localhost"):
        return LOCAL_URLS[0]
    try:
        tbl = json.loads(HOSTS_JSON.read_text())
        url = str(((tbl.get("hosts") or {}).get(h.lower()) or {}).get("url") or "")
    except Exception:
        url = ""
    return url.rstrip("/")


def driver_pinned_host(driver_text: str) -> str:
    """The host a driver has hardcoded at top level (`HOST="..."`), or "" if it
    takes one from the environment / sets none. PURE -- self-tested."""
    m = re.search(r'^HOST=(?:"([^"\n]*)"|\'([^\'\n]*)\'|(\S+))\s*$',
                  driver_text, re.M)
    if not m:
        return ""
    val = m.group(1) or m.group(2) or m.group(3) or ""
    # A driver that already defers to the environment is not "pinned".
    if "${" in val or val.startswith("$"):
        return ""
    return val.rstrip("/")


def host_mismatch(queue_host: str, driver_text: str) -> str:
    """"" when the lane the queue pinned and the host the driver will hit agree
    (or cannot be compared); otherwise a human-readable reason to abort. PURE."""
    want = resolve_host(queue_host)
    if not want:
        return ""   # no lane pinned -> the driver owns the host, as before
    got = driver_pinned_host(driver_text)
    if not got:
        return ""   # driver defers to the environment -> our export wins
    if got.rstrip("/") == want:
        return ""
    return (f"queue pinned --host {queue_host!r} ({want}) but the driver hardcodes "
            f"HOST={got!r}: the eval would run on the WRONG GPU and the queue view "
            f"would be wrong about which card is busy. Change the driver's line to "
            f'HOST="${{BAKEOFF_HOST:-{got}}}" (it then honours the lane), or set '
            f"BAKEOFF_ALLOW_HOST_MISMATCH=1 if the driver really owns its hosts.")
DEFAULT_ROSTER_ONLY = ("qwen3:32b qwen3:32b-q8_0 qwen2.5-coder:32b devstral-small-2:24b "
                       "davidau-qwen38-mtp:q4_K_M rombos-coder-v2.5:q4_K_M")

def parse_max_s(task_text: str, env_val=None) -> int:
    """Wall-clock cap (seconds) for the driver: a `MAX_S=<n>` line in the task file, else
    $BAKEOFF_RUNNER_MAX_S, else 0 (= no cap, the old behaviour). PURE. A driver that
    finished its work but then hung (v12 qwen36-debug-r4: DONE at 16:26, still 'running'
    at 0% CPU 2h52 later until killed by hand) otherwise holds the Studio lane forever."""
    for line in (task_text or "").splitlines():
        s = line.strip()
        if s.startswith("MAX_S="):
            try:
                return max(0, int(s[len("MAX_S="):].strip()))
            except ValueError:
                return 0
    try:
        return max(0, int(env_val or 0))
    except ValueError:
        return 0


def _self_test() -> int:
    """Unit-test the lane resolution + the mismatch guard. No GPU, no network."""
    ok = True

    def check(name, got, want):
        nonlocal ok
        if got == want:
            print(f"PASS {name}")
        else:
            ok = False
            print(f"FAIL {name}: got {got!r} want {want!r}")

    check("lane 'unraid' resolves to the shared host table's url",
          resolve_host("unraid"), "http://198.51.100.145:11434")
    check("the studio lanes resolve to local", resolve_host("studio-db"), LOCAL_URLS[0])
    check("a url passes through", resolve_host("http://x:1/"), "http://x:1")
    check("no --host -> the driver owns the host (unchanged behaviour)",
          resolve_host(""), "")
    _pinned = 'RUN_TAG=v10\nHOST="http://localhost:11434"\nBACKEND=macstudio\n'
    _defers = 'HOST="${BAKEOFF_HOST:-http://localhost:11434}"\n'
    check("a hardcoded driver HOST is detected", driver_pinned_host(_pinned),
          "http://localhost:11434")
    check("a driver that defers to the env is NOT pinned",
          driver_pinned_host(_defers), "")
    # THE GUARD BITES: unraid lane + a localhost-pinned driver must abort.
    _why = host_mismatch("unraid", _pinned)
    check("unraid lane + localhost-pinned driver -> ABORT (names both hosts and the fix)",
          (bool(_why), "198.51.100.145" in _why, "BAKEOFF_HOST" in _why), (True, True, True))
    # REVERT-STYLE: the same driver on the lane it actually pins is fine, and the
    # deferring driver is fine on ANY lane -- so the guard is discriminating, not
    # a blanket refusal.
    check("...but the same driver on the studio lane is fine",
          host_mismatch("studio", _pinned), "")
    check("...and a deferring driver is fine on the unraid lane",
          host_mismatch("unraid", _defers), "")
    check("MAX_S line in the task file sets the cap", parse_max_s("DRIVER=x\nMAX_S=14400\n"), 14400)
    check("no MAX_S -> env value", parse_max_s("ROSTER_ONLY=a", "90"), 90)
    check("neither -> 0 (no cap)", parse_max_s("ROSTER_ONLY=a", None), 0)
    check("garbage -> 0", parse_max_s("MAX_S=abc"), 0)
    check("an unresolvable lane does not abort (the driver's host stands)",
          host_mismatch("nosuchbox", _pinned), "")
    print("SELF_TEST_OK" if ok else "SELF_TEST_FAILED")
    return 0 if ok else 1


def main():
    if "--self-test" in sys.argv[1:]:
        return _self_test()
    ap = argparse.ArgumentParser()
    # Queue-supplied flags. Accepted for contract compatibility; the driver owns
    # its own model/host/ctx, so these are informational only.
    ap.add_argument("--model", default="")
    ap.add_argument("--host", default="")
    ap.add_argument("--num-ctx", default="")
    ap.add_argument("--cwd", default="")
    ap.add_argument("--task-file", default="")
    args, _ = ap.parse_known_args()

    # The queue passes no arbitrary env, but it DOES pass --task-file, which we
    # own. So a head-to-head / alternate-cell run configures itself there: lines
    # `DRIVER=<path>` and `ROSTER_ONLY=<space-separated tags>` override the
    # defaults below. Absent (the original bulk enqueue), behaviour is unchanged.
    driver = DRIVER
    max_s = 0
    roster = os.environ.get("BAKEOFF_ROSTER_ONLY", DEFAULT_ROSTER_ONLY)
    if args.task_file and Path(args.task_file).is_file():
        for line in Path(args.task_file).read_text().splitlines():
            s = line.strip()
            if s.startswith("DRIVER="):
                driver = s[len("DRIVER="):].strip()
            elif s.startswith("ROSTER_ONLY="):
                roster = s[len("ROSTER_ONLY="):].strip()

    try:
        max_s = parse_max_s(Path(args.task_file).read_text() if args.task_file else "",
                            os.environ.get("BAKEOFF_RUNNER_MAX_S"))
    except OSError:
        max_s = 0
    if not Path(driver).exists():
        print(f"[bakeoff-runner] driver not found: {driver}", file=sys.stderr)
        return 1

    env = dict(os.environ)
    env["ROSTER_ONLY"] = roster

    # UNRAID OVERFLOW: honour the lane the queue pinned. BAKEOFF_HOST is the
    # contract for a driver that defers (`HOST="${BAKEOFF_HOST:-...}"`);
    # OLLAMA_HOST covers the ollama CLI calls the drivers make for
    # load/unload/ps. A provable mismatch aborts rather than mis-measuring.
    host_url = resolve_host(args.host)
    if host_url:
        env["BAKEOFF_HOST"] = host_url
        env.setdefault("BAKEOFF_LANE", str(args.host))
        env["OLLAMA_HOST"] = host_url
        try:
            why = host_mismatch(args.host, Path(driver).read_text(errors="replace"))
        except OSError:
            why = ""
        if why:
            if ALLOW_HOST_MISMATCH:
                print(f"[bakeoff-runner] HOST MISMATCH (allowed by "
                      f"BAKEOFF_ALLOW_HOST_MISMATCH=1): {why}", file=sys.stderr, flush=True)
            else:
                print(f"[bakeoff-runner] ABORT -- {why}", file=sys.stderr, flush=True)
                return 2
    elif args.host:
        print(f"[bakeoff-runner] WARNING: --host {args.host!r} resolves to no url in "
              f"{HOSTS_JSON} -- the driver's own host stands", file=sys.stderr, flush=True)

    print(f"[bakeoff-runner] launching driver={driver} (queue model hint={args.model!r}, "
          f"lane={args.host or '-'} -> {host_url or 'driver-owned'}); "
          f"ROSTER_ONLY={env['ROSTER_ONLY']}", flush=True)

    proc = subprocess.Popen(["bash", driver], env=env)

    # Forward SIGTERM/SIGINT to the driver so queue cancel/pause stops the run
    # cleanly (the driver's per-cell timeout watchers already handle child kills).
    def _forward(signum, _frame):
        try:
            proc.send_signal(signum)
        except ProcessLookupError:
            pass
    signal.signal(signal.SIGTERM, _forward)
    signal.signal(signal.SIGINT, _forward)

    if not max_s:
        return proc.wait()
    try:
        return proc.wait(timeout=max_s)
    except subprocess.TimeoutExpired:
        print(f"[bakeoff-runner] driver still running after MAX_S={max_s}s -- SIGTERM "
              f"(hung post-run step?); the queue row fails with exit 124", file=sys.stderr, flush=True)
        _forward(signal.SIGTERM, None)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        return 124

if __name__ == "__main__":
    sys.exit(main())
