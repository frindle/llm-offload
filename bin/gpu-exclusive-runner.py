#!/usr/bin/env python3
"""gpu-exclusive-runner.py -- run ONE non-LLM shell job with a queue lane's GPU to itself.

Launched only by ~/bin/ollama-queue.py (it is in ALLOWED_RUNNERS) for a job created
with `ollama-queue.py enqueue-gpu`. The queue has already given this job the lane
(e.g. `unraid`, the RTX 3080 that the Unraid Ollama pre-gate also uses), so no other
queue job runs on that lane until this process exits. Ollama keeps models resident
after a request (keep_alive), so a free lane can still hold ~10GB of VRAM. This runner
makes the GPU actually free before it starts the command:

  1. EVICT: every model in the lane's Ollama /api/ps is unloaded (keep_alive 0), and
     /api/ps is polled until it is empty. If it cannot be emptied or the host does not
     answer, the job FAILS without running the command (fail closed).
  2. VRAM CHECK (optional): `vram_check` is a shell command that prints the GPU's used
     MiB; it is polled until the value is <= `vram_max_used_mib`. Otherwise the job fails.
  3. RUN `cmd` (bash -c) in its own process group, output streamed to stdout (which
     the queue points at the job's live log). A watcher polls /api/ps every 30s and
     logs an INTRUSION when anything loads a model on that host meanwhile (anything
     outside the queue, e.g. an embedding call). It logs it and does not kill anything.
  4. STOP/TIMEOUT: on SIGTERM/SIGINT (queue `stop`, dashboard stop, daemon shutdown)
     or after `timeout_s`, the command's process group gets SIGTERM, then SIGKILL after
     30s, and then `on_abort` runs (e.g. an ssh that stops the remote side, which a
     killed local ssh client does not).
  5. RESULT: <task-file stem>.result.json is written next to the spec. The queue
     reads it if the daemon restarted mid-run, so an exclusive job is never re-run
     because nobody saw its exit code. Exit 0 when the command exited 0, else 1. It
     never exits 3/5, which the queue reads as the worker's paused/unconverged codes.

Contract (ALLOWED_RUNNERS): --model --host --num-ctx --cwd --task-file. --model is the
"gpu-exclusive" placeholder and --num-ctx is ignored; --host is the lane's Ollama URL.
Spec (task file, JSON): {"cmd": str, "timeout_s": int, "on_abort": str|null,
"evict": bool, "vram_check": str|null, "vram_max_used_mib": int|null}.

Self-test: gpu-exclusive-runner.py --self-test (stub Ollama + stub commands, no GPU).
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

EVICT_WAIT_S = 90
VRAM_WAIT_S = 180   # was 60: one ssh timeout alone is ~75 s; allow a retry
WATCH_INTERVAL_S = 30
KILL_GRACE_S = 30
ON_ABORT_TIMEOUT_S = 180
DEFAULT_TIMEOUT_S = 7200
# Never 3 (EXIT_CODE_PAUSED) or 5 (EXIT_CODE_DONE_UNCONVERGED): the queue's reap reads
# those as the worker's resumable-pause / verify-passed-unconverged outcomes.
RC_OK, RC_FAIL = 0, 1


def log(msg):
    print(f"[gpu-job {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _http_json(url, body=None, timeout=15):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw.strip() else {}


def resident_models(host):
    """Names of the models the host's Ollama has loaded. Raises when the host does not
    answer: an unreadable /api/ps can't show the GPU is free."""
    d = _http_json(host.rstrip("/") + "/api/ps")
    return [m.get("name") or m.get("model") for m in (d.get("models") or [])]


def evict_all(host, wait_s=EVICT_WAIT_S, poll=2.0, sleep=time.sleep, clock=time.time):
    """Unload every resident model and wait for /api/ps to read empty.
    Returns (ok, evicted_names, why)."""
    try:
        names = resident_models(host)
    except Exception as e:  # noqa: BLE001
        return False, [], f"cannot read {host}/api/ps ({type(e).__name__}: {e}) -- refusing to run"
    for n in names:
        try:
            _http_json(host.rstrip("/") + "/api/generate", {"model": n, "keep_alive": 0}, timeout=60)
            log(f"evict: asked {host} to unload {n} (keep_alive 0)")
        except Exception as e:  # noqa: BLE001
            log(f"evict: unload request for {n} failed ({type(e).__name__}: {e})")
    deadline = clock() + wait_s
    while True:
        try:
            left = resident_models(host)
        except Exception as e:  # noqa: BLE001
            return False, names, f"cannot re-read {host}/api/ps after evict ({e})"
        if not left:
            return True, names, ("nothing was resident" if not names
                                 else f"unloaded {', '.join(names)}")
        if clock() >= deadline:
            return False, names, f"still resident after {wait_s}s: {', '.join(left)}"
        sleep(poll)


def vram_used_mib(check_cmd, timeout=90):
    out = subprocess.run(["bash", "-c", check_cmd], capture_output=True, text=True,
                         timeout=timeout, stdin=subprocess.DEVNULL)
    if out.returncode != 0:
        raise RuntimeError(f"vram_check exited {out.returncode}: {out.stderr.strip()[:200]}")
    return int(out.stdout.strip().splitlines()[0].strip())


def wait_vram_free(check_cmd, max_mib, wait_s=VRAM_WAIT_S, poll=5.0, sleep=time.sleep,
                   clock=time.time, reader=vram_used_mib):
    deadline = clock() + wait_s
    last = None
    while True:
        try:
            last = reader(check_cmd)
        except Exception as e:  # noqa: BLE001
            # A check that can't run (ssh to the card's host timed out -- live 2026-10-09,
            # bo-v12-strata-bulk-r3: "connect to host ... port 22: Operation timed out")
            # is not "the GPU is busy": retry until the deadline instead of failing the
            # whole exclusive job on one transient network blip. Still fail closed: the
            # command never runs unless a reading <= max_mib is obtained.
            if clock() >= deadline:
                return False, None, f"vram_check failed ({type(e).__name__}: {e})"
            log(f"vram: check failed ({type(e).__name__}: {str(e)[:120]}) -- retrying")
            sleep(poll)
            continue
        if last <= max_mib:
            return True, last, f"GPU shows {last} MiB used (<= {max_mib})"
        if clock() >= deadline:
            return False, last, f"GPU still shows {last} MiB used after {wait_s}s (> {max_mib})"
        sleep(poll)


class Job:
    def __init__(self, spec, host, result_path, evict_wait_s=EVICT_WAIT_S):
        self.spec = spec
        self.evict_wait_s = evict_wait_s
        self.host = host
        self.result_path = Path(result_path)
        self.proc = None
        self.abort_reason = None
        self.intrusions = []
        self._stop = threading.Event()

    # -- lifecycle -----------------------------------------------------------------
    def _kill_group(self):
        p = self.proc
        if p is None or p.poll() is not None:
            return
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except OSError:
            return
        t0 = time.time()
        while p.poll() is None and time.time() - t0 < KILL_GRACE_S:
            time.sleep(0.5)
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass

    def _on_abort(self):
        cmd = self.spec.get("on_abort")
        if not cmd:
            return None
        log("on_abort: running cleanup command")
        try:
            r = subprocess.run(["bash", "-c", cmd], timeout=ON_ABORT_TIMEOUT_S,
                               stdin=subprocess.DEVNULL)
            log(f"on_abort: exit {r.returncode}")
            return r.returncode
        except Exception as e:  # noqa: BLE001
            log(f"on_abort: failed ({type(e).__name__}: {e})")
            return None

    def _signal(self, signum, _frame):
        if self.abort_reason is None:
            self.abort_reason = f"signal {signum}"
            log(f"received signal {signum} -- stopping the command")
        self._stop.set()

    def _watch(self):
        while not self._stop.wait(WATCH_INTERVAL_S):
            try:
                names = resident_models(self.host)
            except Exception:
                continue
            if names:
                stamp = datetime.now(timezone.utc).isoformat()
                self.intrusions.append({"at": stamp, "models": names})
                log(f"INTRUSION: {', '.join(names)} loaded on {self.host} during the exclusive "
                    f"window (not from the queue) -- VRAM is contended; logged, not killed")

    def write_result(self, **kw):
        rec = {"written_at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(),
               "intrusions": self.intrusions, **kw}
        tmp = self.result_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, indent=2))
        os.replace(tmp, self.result_path)
        return rec

    def run(self):
        signal.signal(signal.SIGTERM, self._signal)
        signal.signal(signal.SIGINT, self._signal)
        started = datetime.now(timezone.utc).isoformat()
        cmd = self.spec.get("cmd")
        if not isinstance(cmd, str) or not cmd.strip():
            self.write_result(rc=None, exit=RC_FAIL, phase="spec", why="spec has no cmd",
                              started=started)
            log("spec has no cmd -- nothing to run")
            return RC_FAIL
        evicted = []
        if self.spec.get("evict", True):
            ok, evicted, why = evict_all(self.host, wait_s=self.evict_wait_s)
            log(f"evict: {why}")
            if not ok:
                self.write_result(rc=None, exit=RC_FAIL, phase="evict", why=why,
                                  evicted=evicted, started=started)
                return RC_FAIL
        if self.spec.get("vram_check"):
            mx = int(self.spec.get("vram_max_used_mib") or 1024)
            ok, used, why = wait_vram_free(self.spec["vram_check"], mx)
            log(f"vram: {why}")
            if not ok:
                self.write_result(rc=None, exit=RC_FAIL, phase="vram", why=why,
                                  evicted=evicted, vram_used_mib=used, started=started)
                return RC_FAIL
        if self._stop.is_set():
            self.write_result(rc=None, exit=RC_FAIL, phase="pre-run", why=self.abort_reason,
                              evicted=evicted, started=started)
            return RC_FAIL
        timeout_s = int(self.spec.get("timeout_s") or DEFAULT_TIMEOUT_S)
        log(f"running command (timeout {timeout_s}s); the lane stays exclusive until it exits")
        self.write_result(rc=None, exit=None, phase="running", evicted=evicted, started=started)
        self.proc = subprocess.Popen(["bash", "-c", cmd], start_new_session=True,
                                     stdin=subprocess.DEVNULL)
        watcher = threading.Thread(target=self._watch, daemon=True)
        watcher.start()
        t0 = time.time()
        timed_out = False
        while self.proc.poll() is None:
            if self._stop.wait(1.0):
                break
            if time.time() - t0 > timeout_s:
                timed_out = True
                self.abort_reason = f"timeout after {timeout_s}s"
                log(self.abort_reason)
                break
        aborted = self.proc.poll() is None
        abort_rc = None
        if aborted:
            self._kill_group()
            abort_rc = self._on_abort()
        self._stop.set()
        rc = self.proc.wait()
        if timed_out:
            rc = 124
        out = RC_OK if (rc == 0 and not aborted) else RC_FAIL
        rec = self.write_result(rc=rc, exit=out, phase="finished", aborted=aborted,
                                abort_reason=self.abort_reason, on_abort_rc=abort_rc,
                                evicted=evicted, started=started,
                                duration_s=round(time.time() - t0, 1))
        log(f"command exited {rc}{' (aborted: ' + str(self.abort_reason) + ')' if aborted else ''}"
            f"; intrusions={len(rec['intrusions'])}; lane released -> exit {out}")
        return out


def result_path_for(task_file):
    p = Path(task_file)
    return p.with_name(p.stem + ".result.json")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", required=True)
    ap.add_argument("--host", required=True)
    ap.add_argument("--num-ctx", default=None)
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--task-file", required=True)
    a = ap.parse_args(argv)
    spec = json.loads(Path(a.task_file).read_text())
    log(f"exclusive GPU job on {a.host}: {spec.get('summary') or spec.get('cmd', '')[:120]}")
    return Job(spec, a.host, result_path_for(a.task_file)).run()


# ------------------------------------------------------------------------------------
def _self_test():
    import http.server
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok &= good
        print(("PASS " if good else "FAIL ") + name + ("" if good else f"  got={got!r} want={want!r}"))

    loaded = {"models": ["qwen3:14b"]}
    unload_calls = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj):
            b = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            self._send({"models": [{"name": n} for n in loaded["models"]]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            unload_calls.append(body)
            if body.get("keep_alive") == 0 and not loaded.get("sticky"):
                loaded["models"] = [m for m in loaded["models"] if m != body["model"]]
            self._send({})

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{srv.server_address[1]}"
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # 1. eviction unloads the resident model, then the command runs
        marker = td / "ran"
        j = Job({"cmd": f"test \"$(curl -s {host}/api/ps)\" = '{{\"models\": []}}' && touch {marker}"},
                host, td / "a.result.json")
        rc = j.run()
        check("evict: resident model unloaded with keep_alive 0",
              unload_calls[:1], [{"model": "qwen3:14b", "keep_alive": 0}])
        check("evict: command ran only after /api/ps read empty", marker.exists(), True)
        check("success: exit 0", rc, 0)
        res = json.loads((td / "a.result.json").read_text())
        check("success: result records rc 0 and the evicted model",
              (res["rc"], res["evicted"], res["phase"]), (0, ["qwen3:14b"], "finished"))
        # 2. fail closed: a model that will not unload -> command never runs
        loaded.update(models=["stuck:7b"], sticky=True)
        marker2 = td / "ran2"
        check("evict: a model that stays resident -> not ok",
              evict_all(host, wait_s=1, poll=0.2)[0], False)
        j2 = Job({"cmd": f"touch {marker2}"}, host, td / "b.result.json", evict_wait_s=1)
        rc2 = j2.run()
        check("fail closed: command NOT run when eviction fails", marker2.exists(), False)
        check("fail closed: exit 1", rc2, 1)
        check("fail closed: result phase=evict",
              json.loads((td / "b.result.json").read_text())["phase"], "evict")
        loaded.update(models=[], sticky=False)
        # 3. unreachable host -> fail closed
        j3 = Job({"cmd": "true"}, "http://127.0.0.1:9", td / "c.result.json")
        check("unreachable Ollama -> exit 1 without running", j3.run(), 1)
        # 4. non-zero command -> exit 1, never 3/5
        j4 = Job({"cmd": "exit 3"}, host, td / "d.result.json")
        check("command exit 3 -> runner exit 1 (never the worker's pause code)", j4.run(), 1)
        check("command exit 3 recorded as rc 3",
              json.loads((td / "d.result.json").read_text())["rc"], 3)
        # 5. timeout -> group killed, on_abort runs, rc 124
        ab = td / "aborted"
        j5 = Job({"cmd": "sleep 30", "timeout_s": 1, "on_abort": f"touch {ab}"}, host,
                 td / "e.result.json")
        t0 = time.time()
        check("timeout -> exit 1", j5.run(), 1)
        r5 = json.loads((td / "e.result.json").read_text())
        check("timeout: rc 124, aborted, on_abort ran",
              (r5["rc"], r5["aborted"], ab.exists()), (124, True, True))
        check("timeout: child group killed promptly", time.time() - t0 < 15, True)
        # 6. vram gate
        check("vram: reader under the limit -> ok",
              wait_vram_free("x", 500, reader=lambda c: 100)[0], True)
        _calls = []

        def _flaky(c):
            _calls.append(1)
            if len(_calls) < 3:
                raise RuntimeError("ssh: connect to host x port 22: Operation timed out")
            return 4
        check("vram: transient reader failures are retried, then ok",
              wait_vram_free("x", 500, poll=0, sleep=lambda s: None, reader=_flaky)[0], True)
        _t = [0]

        def _dead(c):
            raise RuntimeError("down")

        def _clk():
            _t[0] += 50
            return _t[0]
        check("vram: a reader that never works still fails closed at the deadline",
              wait_vram_free("x", 500, wait_s=120, poll=0, sleep=lambda s: None, clock=_clk,
                             reader=_dead)[0], False)
        check("vram: reader stays over the limit -> not ok",
              wait_vram_free("x", 500, wait_s=0, reader=lambda c: 9000)[0], False)
        # 7. SIGTERM -> abort path
        ab2 = td / "aborted2"
        j7 = Job({"cmd": "sleep 30", "on_abort": f"touch {ab2}"}, host, td / "f.result.json")
        threading.Timer(1.5, lambda: j7._signal(signal.SIGTERM, None)).start()
        check("SIGTERM -> exit 1", j7.run(), 1)
        r7 = json.loads((td / "f.result.json").read_text())
        check("SIGTERM: aborted + on_abort ran", (r7["aborted"], ab2.exists()), (True, True))
    srv.shutdown()
    print("SELF_TEST_OK" if ok else "SELF_TEST_FAILED")
    return ok


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(0 if _self_test() else 1)
    sys.exit(main())
