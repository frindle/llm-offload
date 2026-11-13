"""ollama-queue: the Darkbloom sandbox-hold wait, the provider-log down cause and the path-access gate (2026-11-02).

Live: Darkbloom lends the machine to sandbox jobs (addon_hold lines in ~/.darkbloom/provider.log). The model is
unloaded while /v1/models still lists it, so a launch would meet modelNotLoaded. And while the screen is locked
macOS freezes a NEW process's first touch of ~/Desktop (every dispatch worktree's .git points there), so a job
launched then hangs at its first git call (50b475235c58, 20:05-21:37). The queue now reads the provider log
outside the state lock and waits out a hold, and probes a job's paths in a throwaway child before it launches.

Fake HOME, temp files and child probes only: never the real provider log, the real queue state, Darkbloom,
BloomGauge or ESCALATIONS.md.

    python3 -m pytest -q ~/bin/test_queue_path_gate.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
M = "qwen3.6-35b-a3b-vl-mtp-mxfp8"


def _load_queue():
    home = Path(tempfile.mkdtemp(prefix="pathgate-home-"))
    (home / ".darkbloom").mkdir()
    (home / "bin").mkdir()
    (home / ".darkbloom" / "local.json").write_text('{"api_key": "k", "base_url": "http://127.0.0.1:1"}')
    old = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
    try:
        sp = importlib.util.spec_from_file_location("q_pathgate", HERE / "ollama-queue.py")
        m = importlib.util.module_from_spec(sp)
        sys.modules["q_pathgate"] = m
        sp.loader.exec_module(m)
        return m, home
    finally:
        if old is not None:
            os.environ["HOME"] = old


q, FAKE_HOME = _load_queue()


def _load(name, fn):
    sp = importlib.util.spec_from_file_location(name, HERE / fn)
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


def _kill_probes():
    for pr in list(q._PATH_PROBES.values()):
        try:
            pr["proc"].kill()
            pr["proc"].wait(timeout=5)
        except Exception:
            pass
    q._PATH_PROBES.clear()


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Fresh module state per test; incidents to a temp file; alerts recorded, never written."""
    inc = tmp_path / "incidents.jsonl"
    monkeypatch.setenv("INFRA_INCIDENTS_PATH", str(inc))
    monkeypatch.setenv("INFRA_INCIDENTS_SCREEN_LOCKED", "1")
    monkeypatch.setenv("OLLAMA_QUEUE_NO_NOTIFY", "1")
    q._DB_VIEW.clear()
    q._DB_SERVED.update(at=None, ids=None)
    q._DB_TRANS.update(down=None, hold=None)
    _kill_probes()
    q._PATH_VERDICT.clear()
    q._PATH_PRIME.update(at=None, locked=None, open_logged=None)
    q._PATH_ALERTED.clear()
    alerts = []
    monkeypatch.setattr(q, "_control_alert", lambda key, msg, now=None, idx=None, tag=None:
                        alerts.append((key, msg, tag)) or True)
    monkeypatch.setattr(q, "_darkbloom_url", lambda: "http://127.0.0.1:1")
    monkeypatch.setattr(q, "_bloomgauge_control_mode", lambda now=None: "off")
    monkeypatch.setattr(q, "darkbloom_warm_hold", lambda *a, **k: None)
    monkeypatch.setattr(q, "darkbloom_cold_gate", lambda *a, **k: None)
    monkeypatch.setattr(q, "darkbloom_served_ids", lambda *a, **k: pytest.fail("no live /v1/models in tests"))
    monkeypatch.setattr(q, "_STATE_LOCK_DEPTH", 0)

    def incidents(kind=None):
        rows = [json.loads(x) for x in inc.read_text().splitlines()] if inc.exists() else []
        return [r for r in rows if kind is None or r["kind"] == kind]
    yield SimpleNamespace(alerts=alerts, incidents=incidents, tmp=tmp_path)
    _kill_probes()


# ---------------------------------------------------------------- provider.log, as it is really written
START = "Checking for provider update..."
UP = ("2026-11-02T21:37:27-0800 info darkbloom-provider: [HummingbirdCore] Server started and listening on "
      "127.0.0.1:8000")
CACHE = ("2026-11-02T21:17:20-0800 warning darkbloom.ModelScanner.Discovery: [ProviderCore] Failed to read cache "
         "directory /Volumes/NVMe-Models/hf-cache: The file “hf-cache” couldn’t be opened.")
UNAVAIL = "local API: request failed: model_unavailable (modelNotLoaded, model_load)"
DRAINING = '{"coordinator_acknowledged":false,"operation":"provider_drain","in_flight":0,"reason":"draining"}'
DRAINED = '{"coordinator_acknowledged":true,"operation":"provider_drain","in_flight":0,"reason":"drained"}'
BEGIN = ('{"addon":"io.darkbloom.sandbox","in_flight":0,"lease_seconds":60,"operation":"addon_hold",'
         '"state":"draining","transition":"begin"}')
HASH = ("2026-11-02T22:10:06-0800 info darkbloom.WeightHasher: [ProviderCoreFoundation] Computing weight hash "
        f"for {M} (13 files)...")
PREP = ("2026-11-02T22:10:23-0800 info darkbloom.request.prep: normalize_ms=0.039 [ProviderCore] Prompt "
        "preparation")


def hold(tr, lease=60):
    return ('{"addon":"io.darkbloom.sandbox","lease_seconds":%d,"operation":"addon_hold","state":"held",'
            '"transition":"%s"}' % (lease, tr))


def cause(*lines, age=None):
    return q.darkbloom_down_cause_text("\n".join(lines) + "\n", log_age_s=age)


def test_a_live_hold_counts_its_renewals():
    v = cause(START, UP, PREP, BEGIN, hold("held"), hold("renewed"), hold("renewed"), hold("renewed"))
    assert v == {"cause": "sandbox-hold", "hold": True, "lease_s": 60, "renewals": 3, "cache_path": None}


def test_after_a_release_the_in_process_reload_serves_no_cause():
    """22:10, screen locked: the hold released, the same provider hashed the weights and served 17 s later."""
    assert cause(START, UP, BEGIN, hold("held"), hold("renewed"), hold("released"), HASH, PREP)["cause"] is None
    assert cause(START, UP, BEGIN, hold("held"), hold("released"))["cause"] is None


def test_a_hold_whose_log_went_silent_is_over():
    lines = (START, UP, BEGIN, hold("held"), hold("renewed"))
    assert cause(*lines, age=60)["hold"] is True
    assert cause(*lines, age=q.DB_HOLD_STALE_S + 1)["cause"] is None
    long_lease = (START, UP, BEGIN, hold("held", 120), hold("renewed", 120))
    assert cause(*long_lease, age=300)["hold"] is True, "3 leases of 120 s outlast the 180 s floor"
    assert cause(*long_lease, age=361)["hold"] is False


def test_a_hold_logged_before_a_provider_restart_is_not_live():
    assert cause(UP, BEGIN, hold("held"), START)["cause"] == "restarting"
    assert cause(START, BEGIN, hold("held"))["cause"] == "sandbox-hold", "a hold after the banner is"


def test_the_21_16_outage_reads_draining_then_restarting_then_cache_then_up():
    """The real sequence: a hold released, the provider drained and restarted, and the new process could not
    read hf-cache with the screen locked until 21:37."""
    seq = [UP, BEGIN, hold("held"), hold("released"), DRAINING, DRAINED]
    assert cause(*seq)["cause"] == "draining"
    seq.append(START)
    assert cause(*seq)["cause"] == "restarting"
    seq += [CACHE, CACHE]
    v = cause(*seq)
    assert v["cause"] == "model-cache-unreadable" and v["cache_path"] == "/Volumes/NVMe-Models/hf-cache"
    seq.append(UP)
    assert cause(*seq) == {"cause": None, "hold": False, "lease_s": 60, "renewals": 0, "cache_path": None}


def test_model_not_loaded_until_a_request_is_prepared_again():
    assert cause(UP, PREP, UNAVAIL)["cause"] == "model-not-loaded"
    assert cause(UP, PREP, UNAVAIL, PREP)["cause"] is None
    assert cause(UP, UNAVAIL, BEGIN, hold("held"), hold("released"))["cause"] is None, "a release resets it"


def test_junk_never_raises():
    for text in ("", None, "{not json", '{"operation":"addon_hold"', "[1,2]\n{}\n", '{"x": "addon_hold"}'):
        assert q.darkbloom_down_cause_text(text)["cause"] is None


# ---------------------------------------------------------------- the view (files under ~/.darkbloom only)
def slots_line(t, jobs):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) + f" locked=1 vnc=0 want=4 cur=4 jobs={jobs} agent_session=1\n"


def test_view_reads_the_log_tail_and_the_slots_log(env):
    log, slots = env.tmp / "provider.log", env.tmp / "sandbox-slots.log"
    log.write_text("\n".join(["x" * 200] * 400 + [START, UP, BEGIN, hold("held"), hold("renewed")]) + "\n")
    now = log.stat().st_mtime + 5
    slots.write_text(slots_line(now - 400, 0) + slots_line(now - 30, 2))
    v = q.darkbloom_view(now, log_path=log, slots_path=slots)
    assert v["cause"] == "sandbox-hold" and v["sandbox_jobs"] == 2 and v["at"] == now and 4 <= v["log_age_s"] <= 6
    slots.write_text(slots_line(now - 400, 1))
    assert q.darkbloom_view(now, log_path=log, slots_path=slots)["sandbox_jobs"] is None, "a stale slots line"
    slots.write_text("garbage\n")
    assert q.darkbloom_view(now, log_path=log, slots_path=slots)["sandbox_jobs"] is None
    v = q.darkbloom_view(now, log_path=env.tmp / "missing.log", slots_path=env.tmp / "missing")
    assert v == {"cause": None, "hold": False, "unreadable": True, "at": now, "sandbox_jobs": None}


# ---------------------------------------------------------------- the wait notes
def hold_view(now, since=None, jobs=1):
    return {"cause": "sandbox-hold", "hold": True, "at": now, "hold_since": since, "sandbox_jobs": jobs}


def test_hold_wait_note(monkeypatch):
    now = time.time()
    hhmm = time.strftime("%H:%M", time.localtime(now - 300))
    n = q.darkbloom_hold_wait(M, now, hold_view(now, since=now - 300))
    assert n["code"] == "darkbloom-sandbox"
    assert n["short"] == f"darkbloom sandbox hold since {hhmm}: {M} unloaded until it is released"
    assert n["sentence"].startswith("INFRA WAIT: Darkbloom lent the machine to a sandbox job")
    assert "(1 sandbox job running)" in n["sentence"] and "never run darkbloom or sandbox commands by hand" in n["sentence"]
    assert n["waiting_on"]["kind"] == "infra" and n["waiting_on"]["since"] == now - 300
    assert "(2 sandbox jobs running)" in q.darkbloom_hold_wait(M, now, hold_view(now, jobs=2))["sentence"]
    assert "running)" not in q.darkbloom_hold_wait(M, now, hold_view(now, jobs=None))["sentence"]
    assert q.darkbloom_hold_wait(M, now, hold_view(now - q.DB_VIEW_FRESH_S - 1)) is None, "a stale view"
    assert q.darkbloom_hold_wait(M, now, {"cause": None, "hold": False, "at": now}) is None
    monkeypatch.setattr(q, "SANDBOX_HOLD_WAIT", False)
    assert q.darkbloom_hold_wait(M, now, hold_view(now)) is None, "the kill switch"


def test_launch_waits_out_a_hold_even_when_v1_models_lists_the_model():
    now = time.time()
    q._DB_VIEW.update(hold_view(now, since=now - 60))
    for ready in (True, False):
        assert q.darkbloom_launch_wait(M, now, ready=ready)["code"] == "darkbloom-sandbox"
    q._DB_VIEW.clear()
    assert q.darkbloom_launch_wait(M, now, ready=True) is None


def test_launch_wait_names_the_unreadable_cache_and_the_lock():
    now = time.time()
    q._DB_VIEW.update(cause="model-cache-unreadable", cache_path="/Volumes/NVMe-Models/hf-cache", locked=True, at=now)
    n = q.darkbloom_launch_wait(M, now, ready=False)
    assert n["code"] == "infra-darkbloom" and n["short"].startswith(f"darkbloom not serving {M} -- fix: nothing to run")
    assert n["sentence"].startswith(f"INFRA WAIT: darkbloom not serving {M} at http://127.0.0.1:1 (the provider "
                                    "cannot read its model cache /Volumes/NVMe-Models/hf-cache -- the screen is locked")
    assert "fix: nothing to run: the provider retries by itself" in n["sentence"]
    assert n["waiting_on"]["cause"] == "model-cache-unreadable"
    q._DB_VIEW["locked"] = False
    n = q.darkbloom_launch_wait(M, now, ready=False)
    assert "check the volume is mounted and readable" in n["sentence"] and "fix: needs %s if it persists" % q.queue_config.operator_name() in n["sentence"]


def test_launch_wait_without_a_view_keeps_the_old_reason():
    n = q.darkbloom_launch_wait(M, time.time(), ready=False)
    assert n["sentence"].startswith(f"INFRA WAIT: darkbloom not serving {M}") and "fix:" in n["sentence"]
    assert "(BloomGauge serves another model, or is switching)" in n["sentence"] and n["waiting_on"]["cause"] is None
    q._DB_VIEW.update(cause="restarting", at=time.time() - q.DB_VIEW_FRESH_S - 5)
    assert "(BloomGauge serves another model" in q.darkbloom_launch_wait(M, time.time(), ready=False)["sentence"]


# ---------------------------------------------------------------- the prime (outside the lock) and its incidents
def test_prime_records_a_hold_once_and_its_release(env):
    t0, fetched = 1_793_690_000.0, []
    fetch = lambda: fetched.append(1) or [M]
    v = q.prime_darkbloom_view(t0, fetch=fetch, view=hold_view(t0, jobs=1) | {"lease_s": 60, "renewals": 2})
    assert v["hold_since"] == t0 and q._DB_VIEW["hold_since"] == t0 and fetched == [1]
    q.prime_darkbloom_view(t0 + 10, fetch=fetch, view=hold_view(t0 + 10))
    assert fetched == [1], "/v1/models at most every DB_SERVED_EVERY_S" and q._DB_VIEW["hold_since"] == t0
    q.prime_darkbloom_view(t0 + 20, fetch=fetch, view={"cause": None, "hold": False, "unreadable": True, "at": t0 + 20})
    assert env.incidents("sandbox-release") == [], "an unreadable log does not end a hold"
    q.prime_darkbloom_view(t0 + 70, fetch=fetch, view={"cause": None, "hold": False, "at": t0 + 70})
    [h] = env.incidents("sandbox-hold")
    assert h["component"] == "queue" and h["lease_s"] == 60 and h["renewals"] == 2 and h["sandbox_jobs"] == 1
    [r] = env.incidents("sandbox-release")
    assert r["held_for_s"] == 70 and "then" not in r and fetched == [1, 1]
    assert env.incidents("darkbloom-down") == [], "a hold keeps /v1/models listing the model"


def test_prime_pairs_darkbloom_down_and_up_with_the_cause(env):
    t0, ids = 1_793_690_000.0, [None]
    fetch = lambda: ids[0]
    view = {"cause": "model-cache-unreadable", "cache_path": "/Volumes/NVMe-Models/hf-cache", "hold": False}
    v = q.prime_darkbloom_view(t0, fetch=fetch, view=view)
    assert v["locked"] is True, "the screen state rides with a cache failure"
    ids[0] = [M]
    q.prime_darkbloom_view(t0 + 40, fetch=fetch, view={"cause": None, "hold": False})
    [d] = env.incidents("darkbloom-down")
    assert d["cause"] == "model-cache-unreadable" and d["cache_path"] == "/Volumes/NVMe-Models/hf-cache" and d["screen"] is True
    [u] = env.incidents("darkbloom-up")
    assert u["down_for_s"] == 40 and u["cause"] == "model-cache-unreadable"


def test_a_cause_found_after_the_outage_began_is_kept_for_the_up_record(env):
    t0, ids = 1_793_690_000.0, [None]
    q.prime_darkbloom_view(t0, fetch=lambda: ids[0], view={"cause": None, "hold": False})
    q.prime_darkbloom_view(t0 + 40, fetch=lambda: ids[0], view={"cause": "restarting", "hold": False})
    ids[0] = [M]
    q.prime_darkbloom_view(t0 + 80, fetch=lambda: ids[0], view={"cause": None, "hold": False})
    assert [r.get("cause") for r in env.incidents()] == [None, "restarting"]
    assert len(env.incidents("darkbloom-down")) == 1


def test_prime_does_nothing_under_the_lock_and_never_raises(env, monkeypatch, capsys):
    monkeypatch.setattr(q, "_STATE_LOCK_DEPTH", 1)
    assert q.prime_darkbloom_view(1.0, fetch=lambda: pytest.fail("no fetch under the lock")) == {}
    monkeypatch.setattr(q, "_STATE_LOCK_DEPTH", 0)
    q._DB_VIEW.update(cause="restarting", at=5.0)

    def boom():
        raise RuntimeError("socket")
    assert q.prime_darkbloom_view(100.0, fetch=boom, view={"cause": None}) == {}
    assert "darkbloom view error" in capsys.readouterr().err and q._DB_VIEW["cause"] == "restarting"
    monkeypatch.setattr(q, "_darkbloom_url", lambda: None)
    assert q.prime_darkbloom_view(200.0, fetch=boom) == {}, "Darkbloom not configured"


def test_served_ids_under_the_lock_come_from_the_prime(monkeypatch):
    now = 1_793_690_000.0
    monkeypatch.setattr(q, "darkbloom_served_ids", lambda *a, **k: ["live"])
    assert q.darkbloom_served_ids_locked(now) == ["live"], "outside the lock: a live fetch"
    monkeypatch.setattr(q, "_STATE_LOCK_DEPTH", 1)
    assert q.darkbloom_served_ids_locked(now) is q.DB_SERVED_UNKNOWN
    q._DB_SERVED.update(at=now - 10, ids=[M])
    assert q.darkbloom_served_ids_locked(now) == [M]
    q._DB_SERVED.update(at=now - 10, ids=None)
    assert q.darkbloom_served_ids_locked(now) is None, "a fresh 'down' answer"
    q._DB_SERVED.update(at=now - q.DB_SERVED_FRESH_S - 1, ids=[M])
    assert q.darkbloom_served_ids_locked(now) is q.DB_SERVED_UNKNOWN
    monkeypatch.setattr(q, "DB_PROBE_OUTSIDE_LOCK", False)
    assert q.darkbloom_served_ids_locked(now) == ["live"], "the old behaviour behind its switch"


# ---------------------------------------------------------------- the source: where each piece runs
SRC = (HERE / "ollama-queue.py").read_text()


def test_the_drift_check_reads_the_primed_ids_not_the_network():
    i = SRC.index("_sid = darkbloom_served_ids_locked() if _need else None")
    window = SRC[i - 1200:i + 1500]
    assert "_sid is not DB_SERVED_UNKNOWN" in window and "darkbloom_served_ids()" not in window


def test_the_primes_run_before_the_lock_and_the_gate_before_any_path_use():
    lock = SRC.index("        with _Locked() as lock:\n            state = lock.load()\n            changed = False")
    a, b, r = (SRC.rindex(s, 0, lock) for s in ("prime_darkbloom_view()", "prime_path_access()",
                                                "prime_darkbloom_ready()"))
    assert lock - 1500 < a < b < r < lock
    gate = SRC.index("_paw = path_access_wait(job, time.time())")
    assert gate < SRC.index("revalidate_at_pop(job)", gate) and gate < SRC.index('tree_lock_try(job["cwd"])', gate)
    assert SRC.index("_dbw = darkbloom_launch_wait(_dbm, time.time())") < gate
    assert lock < SRC.index("_pbl = path_blocked_list()", lock) < gate


# ---------------------------------------------------------------- the path-access gate
def test_job_access_probes():
    assert q.job_access_probes({"cwd": "/w/x", "task_file": "/r/spec.json", "runner": "/b/mini-swe-runner.py"}) == [
        ("path", "/w/x"), ("spec", "/r/spec.json")]
    assert q.job_access_probes({"cwd": "/w/x", "task_file": "/w/x/task.md"}) == [("path", "/w/x"), ("path", "/w/x/task.md")]
    assert q.job_access_probes({"cwd": "/w/x", "task_file": "/w/x"}) == [("path", "/w/x")]
    assert q.job_access_probes({"cwd": "~/w"})[0] == ("path", str(Path.home() / "w"))
    assert q.job_access_probes({}) == []


def test_probe_output_parsing():
    assert q._path_probe_result('probe "/a"\nprobe "/a/.git/HEAD"\nok\n') == ("ok", "", "/a/.git/HEAD")
    assert q._path_probe_result('probe "/a"\nmissing "/a"\n') == ("missing", '"/a"', "/a")
    assert q._path_probe_result('probe "/a"\nerr "PermissionError: no"\n') == ("err", '"PermissionError: no"', "/a")
    assert q._path_probe_result('probe "/Users/p/Desktop/r/.git/worktrees/w/HEAD"\n') == (
        None, "", "/Users/p/Desktop/r/.git/worktrees/w/HEAD")
    assert q._path_probe_result("") == (None, "", None)
    assert q._path_probe_result('probe "/s.json"\nbase "/u/Desktop/w"\nheld ["/u/Desktop/w", "/u/Desktop"]\n') == (
        "held", "/u/Desktop", "/u/Desktop/w")


def test_path_root_is_a_string_test_only(monkeypatch):
    monkeypatch.setattr(q, "_PATH_TCC_DIRS", ("/u/Desktop", "/u/Documents", "/u/Downloads"))
    assert q._path_root("/u/Desktop") == "/u/Desktop"
    assert q._path_root("/u/Desktop/GitHub Projects/r/.git/HEAD") == "/u/Desktop"
    assert q._path_root("/u/Documents/x/../y") == "/u/Documents"
    assert q._path_root("/u/DesktopOther/x") is None and q._path_root("/u/bin") is None
    assert q._path_root("/Volumes/NVMe-Models/hf-cache") == "/Volumes/NVMe-Models"
    assert q._path_root("/Volumes") is None and q._path_root("") is None and q._path_root(None) is None


def test_the_real_probe_stops_before_a_held_folder(env):
    """It names the path it would have opened and opens nothing under the folder (a spec outside it is read)."""
    desk = env.tmp / "Desk"
    (desk / "wt").mkdir(parents=True)
    spec = env.tmp / "runs" / "spec.json"
    spec.parent.mkdir()
    spec.write_text(json.dumps({"baseline_from": str(desk / "wt")}))
    for k, want in ((("path", str(desk / "wt")), ("held", str(desk), str(desk / "wt"))),
                    (("spec", str(spec)), ("held", str(desk), str(desk / "wt")))):
        q._path_start(k, 1000.0, True, {str(desk)})
        _until_exits(k)
        q._path_drain(q._PATH_PROBES[k])
        out = q._PATH_PROBES.pop(k)["out"]
        assert q._path_probe_result(out) == want, out
        assert ('probe "%s"' % spec) in out if k[0] == "spec" else "probe " not in out


def git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


def test_a_real_probe_follows_the_spec_to_the_worktree_git_dirs(env):
    main = env.tmp / "repo"
    main.mkdir()
    for c in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        git(*c, cwd=main)
    (main / "a.py").write_text("x = 1\n")
    git("add", "a.py", cwd=main)
    git("commit", "-qm", "base", cwd=main)
    wt = env.tmp / "wt"
    git("worktree", "add", "-q", "--detach", str(wt), cwd=main)
    spec = env.tmp / "runs" / "spec.json"
    spec.parent.mkdir()
    spec.write_text(json.dumps({"baseline_from": str(wt)}))
    job = {"id": "r1", "status": "pending", "runner": "/b/mini-swe-runner.py",
           "cwd": str(env.tmp / "mini-work" / "x" / "work"), "task_file": str(spec)}
    now = time.time()
    res = q.prime_path_access(now, jobs=[job, {"id": "d", "status": "done", "cwd": "/never/probed"}], locked=False)
    assert res == {("path", job["cwd"]): "missing", ("spec", str(spec)): "ok"}, res
    held = q._PATH_VERDICT[("spec", str(spec))]["held"]
    assert Path(held).resolve() == (main / ".git" / "config").resolve(), "spec -> worktree -> gitdir -> commondir"
    assert q.path_access_wait(job, now) is None and q._PATH_PROBES == {} and env.incidents() == []
    later = now + q.PATH_PROBE_OK_TTL_S + 1
    q._PATH_PRIME["at"] = later
    assert q.path_access_wait(job, later)["short"] == "infra: checking that a new process can open its files", \
        "an old verdict is not trusted"
    q.prime_path_access(later, jobs=[job], locked=False)
    assert q._PATH_VERDICT[("spec", str(spec))]["at"] == later and q.path_access_wait(job, later) is None


HANG_SRC = r'''
import json, os, sys, time
mode, path = sys.argv[1], sys.argv[2]
sys.stdout.write("probe " + json.dumps(path + "/.git/HEAD") + "\n")
sys.stdout.flush()
end = time.time() + 60
while not os.path.exists(path + ".release") and time.time() < end:
    time.sleep(0.05)
sys.stdout.write("ok\n")
'''


def _until_exits(k, limit=10.0):
    end = time.time() + limit
    while q._PATH_PROBES[k]["proc"].poll() is None and time.time() < end:
        time.sleep(0.05)


@pytest.fixture
def hung(env, monkeypatch):
    """A job whose probe hangs (as a first touch of ~/Desktop does with the screen locked) until released."""
    monkeypatch.setattr(q, "PATH_PROBE_SRC", HANG_SRC)
    monkeypatch.setattr(q, "PATH_PROBE_WAIT_S", 0.3)
    d = env.tmp / "work"
    d.mkdir()
    job = {"id": "h1", "status": "pending", "cwd": str(d)}
    return SimpleNamespace(job=job, k=("path", str(d)), held=str(d) + "/.git/HEAD",
                           release=lambda: (env.tmp / "work.release").write_text("1"))


def test_a_hung_probe_holds_the_job_without_duplicates_then_clears(env, hung):
    t0 = 1_793_690_000.0
    assert q.prime_path_access(t0, jobs=[hung.job], locked=True) == {hung.k: "pending"}
    n = q.path_access_wait(hung.job, t0)
    assert n["code"] == "infra-path-access" and n["short"] == "infra: checking that a new process can open its files"
    assert "the screen is locked" in n["sentence"]
    pid = q._PATH_PROBES[hung.k]["proc"].pid
    assert q.prime_path_access(t0 + 6, jobs=[hung.job], locked=True) == {hung.k: "blocked"}
    assert list(q._PATH_PROBES) == [hung.k] and q._PATH_PROBES[hung.k]["proc"].pid == pid, "never a second probe"
    hhmm = time.strftime("%H:%M", time.localtime(t0))
    n = q.path_access_wait(hung.job, t0 + 6)
    assert n["short"] == f"infra: a new process cannot open {hung.held} (since {hhmm})"
    assert "the job would freeze at its first git call" in n["sentence"] and n["waiting_on"]["screen_locked"] is True
    assert q.path_blocked_list() == [hung.held]
    [b] = env.incidents("path-blocked")
    assert b["path"] == hung.held and b["target"] == hung.k[1] and b["probe"] == "path"
    q.prime_path_access(t0 + 600, jobs=[hung.job], locked=True)
    assert env.alerts == [], "no row for the owner while the screen is locked: unlock clears it"
    hung.release()
    _until_exits(hung.k)
    assert q.prime_path_access(t0 + 900, jobs=[hung.job], locked=True) == {hung.k: "ok"}
    [c] = env.incidents("path-cleared")
    assert c["path"] == hung.held and c["then"] == "ok" and c["blocked_for_s"] == 900
    assert q.path_access_wait(hung.job, t0 + 900) is None and q.path_blocked_list() == []


def test_held_with_the_screen_unlocked_raises_one_infra_row(env, hung):
    t0 = 1_793_690_000.0
    q.prime_path_access(t0, jobs=[hung.job], locked=False)
    q.prime_path_access(t0 + 6, jobs=[hung.job], locked=False)
    q.prime_path_access(t0 + 100, jobs=[hung.job], locked=False)
    assert env.alerts == []
    q.prime_path_access(t0 + 130, jobs=[hung.job], locked=False)
    q.prime_path_access(t0 + 200, jobs=[hung.job], locked=False)
    [(key, msg, tag)] = env.alerts
    assert key == "path-access" and tag == "INFRA" and hung.held in msg and "with the screen unlocked" in msg
    assert not any(ch.isdigit() for ch in msg.replace(hung.held, "").replace("2+ min", "")), "no timestamps"
    n = q.path_access_wait(hung.job, t0 + 200)
    assert "needs %s if it persists" % q.queue_config.operator_name() in n["sentence"] and n["waiting_on"]["screen_locked"] is False
    hung.release()
    _until_exits(hung.k)
    q.prime_path_access(t0 + 300, jobs=[hung.job], locked=False)
    assert q._PATH_ALERTED == {}, "a later hold of the same path alerts again"


def test_held_probes_never_starve_the_rest_and_jobs_that_left_drop_their_verdict(env, hung, monkeypatch):
    """Fable review 2026-11-03: held probes counted against the live cap, so 8 of them stopped every other
    job's probe for the whole lock. Now only unanswered probes count (MAX_INFLIGHT); MAX_PROCS bounds the
    sleeping processes. Paths outside the protected folders, screen unlocked: no folder inference here."""
    monkeypatch.setattr(q, "PATH_PROBE_MAX_INFLIGHT", 2)
    monkeypatch.setattr(q, "PATH_PROBE_MAX_PROCS", 3)
    jobs = []
    for i in range(4):
        d = env.tmp / f"w{i}"
        d.mkdir()
        jobs.append({"id": f"j{i}", "status": "pending", "cwd": str(d)})
    key = lambda i: ("path", jobs[i]["cwd"])
    res = q.prime_path_access(1000.0, jobs=jobs, locked=False)
    assert set(q._PATH_PROBES) == {key(0), key(1)} and set(res.values()) == {"pending"}, "up to the in-flight cap"
    res = q.prime_path_access(1010.0, jobs=jobs, locked=False)
    assert set(q._PATH_PROBES) == {key(0), key(1), key(2)}, "held ones free their in-flight slot; MAX_PROCS still holds"
    assert res == {key(0): "blocked", key(1): "blocked", key(2): "pending", key(3): "pending"}
    (env.tmp / "w0.release").write_text("1")
    _until_exits(key(0))
    res = q.prime_path_access(1020.0, jobs=jobs, locked=False)
    assert res[key(0)] == "ok" and set(q._PATH_PROBES) == {key(1), key(2), key(3)}, "a freed process goes to the next"
    q.prime_path_access(1030.0, jobs=jobs[1:], locked=False)
    assert key(0) not in q._PATH_VERDICT, "a job that left drops its verdict"
    assert q._PATH_VERDICT[key(1)]["state"] == "blocked"


def _held_folder_src(desk):
    """Like the real probe: it stops before a folder it is told is held; under `desk` it hangs until
    `desk`.release exists (a locked screen); anything else answers at once."""
    return r'''
import json, os, sys, time
mode, path, roots = sys.argv[1], sys.argv[2], sys.argv[3:]
for r in roots:
    if path == r or path.startswith(r + "/"):
        print("held " + json.dumps([path, r]), flush=True)
        sys.exit(0)
if path.startswith(%r):
    print("probe " + json.dumps(path + "/.git/HEAD"), flush=True)
    end = time.time() + 60
    while not os.path.exists(%r) and time.time() < end:
        time.sleep(0.05)
print("ok", flush=True)
''' % (desk + "/", desk + ".release")


def test_with_the_screen_locked_one_held_probe_stands_for_its_folder(env, monkeypatch):
    desk = str(env.tmp / "Desk")
    monkeypatch.setattr(q, "_PATH_TCC_DIRS", (desk,))
    monkeypatch.setattr(q, "PATH_PROBE_SRC", _held_folder_src(desk))
    monkeypatch.setattr(q, "PATH_PROBE_WAIT_S", 0.5)
    monkeypatch.setattr(q, "PATH_PROBE_MAX_NEW", 2)
    home = {"id": "h", "status": "pending", "cwd": str(env.tmp / "home-repo")}
    desks = [{"id": f"d{i}", "status": "pending", "cwd": f"{desk}/r{i}"} for i in range(5)]
    jobs, key = desks + [home], (lambda j: ("path", j["cwd"]))
    t0 = 1_793_690_000.0
    res = q.prime_path_access(t0, jobs=jobs, locked=True)
    assert res[key(home)] == "ok", "paths outside the protected folders go first"
    assert set(q._PATH_PROBES) == {key(desks[0])}, "one hangs: no more this tick"
    res = q.prime_path_access(t0 + 6, jobs=jobs, locked=True)
    assert set(res[key(j)] for j in desks) == {"blocked"} and set(q._PATH_PROBES) == {key(desks[0])}, \
        "the rest stop before the folder: one sleeping process, not five"
    assert [q._PATH_VERDICT[key(j)]["via"] for j in desks] == [None] + [desk] * 4
    n = q.path_access_wait(desks[3], t0 + 6)
    assert n["code"] == "infra-path-access" and f"(in {desk}, where a probe is held)" in n["short"]
    assert q.path_access_wait(home, t0 + 6) is None
    assert len(env.incidents("path-blocked")) == 5
    q.prime_path_access(t0 + 60, jobs=jobs, locked=True)
    assert set(q._PATH_PROBES) == {key(desks[0])} and q._PATH_VERDICT[key(desks[3])]["at"] == t0 + 6, \
        "while it is held the inferred verdicts stand: nothing is asked again"
    q.prime_path_access(t0 + 400, jobs=jobs, locked=True)
    assert env.alerts == [], "only a probe's own hold with the screen unlocked raises a row"
    (env.tmp / "Desk.release").write_text("1")
    _until_exits(key(desks[0]))
    res = q.prime_path_access(t0 + 900, jobs=jobs, locked=False)
    assert set(res.values()) == {"ok"} and q._PATH_PROBES == {}, "unlocked: every path asked again, all clear"
    assert len(env.incidents("path-cleared")) == 5 and q.path_blocked_list() == []


def test_inferred_verdicts_are_asked_again_once_no_probe_is_held_there(env, monkeypatch):
    """The held probe was killed stale and is gone: the folder is no longer known held, so the other paths
    are probed again (and one of them becomes the new held probe)."""
    desk = str(env.tmp / "Desk")
    monkeypatch.setattr(q, "_PATH_TCC_DIRS", (desk,))
    monkeypatch.setattr(q, "PATH_PROBE_SRC", _held_folder_src(desk))
    monkeypatch.setattr(q, "PATH_PROBE_WAIT_S", 0.3)
    monkeypatch.setattr(q, "PATH_PROBE_MAX_NEW", 1)
    jobs = [{"id": f"d{i}", "status": "pending", "cwd": f"{desk}/r{i}"} for i in range(2)]
    k0, k1 = ("path", jobs[0]["cwd"]), ("path", jobs[1]["cwd"])
    t0 = 1_793_690_000.0
    q.prime_path_access(t0, jobs=jobs, locked=True)
    q.prime_path_access(t0 + 6, jobs=jobs, locked=True)
    assert q._PATH_VERDICT[k1]["via"] == desk and list(q._PATH_PROBES) == [k0]
    stale = t0 + q.PATH_PROBE_STALE_S
    old = q._PATH_PROBES[k0]["proc"]
    q.prime_path_access(stale + 1, jobs=jobs, locked=True)
    assert list(q._PATH_PROBES) == [k0] and q._PATH_PROBES[k0]["killed"] == stale + 1, \
        "killed, and until it is gone it still says the folder is held"
    old.wait(5)
    monkeypatch.setattr(q, "PATH_PROBE_MAX_NEW", 2)
    q.prime_path_access(stale + 2, jobs=jobs, locked=True)
    assert set(q._PATH_PROBES) == {k0, k1} and q._PATH_PROBES[k0]["proc"] is not old, \
        "gone: no folder is known held, so both paths are asked again"
    q.prime_path_access(stale + 8, jobs=jobs, locked=True)
    assert [q._PATH_VERDICT[k]["via"] for k in (k0, k1)] == [None, None], "each now holds itself"
    assert q._PATH_VERDICT[k1]["since"] == t0 + 6 and len(env.incidents("path-blocked")) == 2, "the same hold"


def test_a_probe_hung_past_the_stale_limit_is_killed_and_asked_again(env, hung):
    t0 = 1_793_690_000.0
    q.prime_path_access(t0, jobs=[hung.job], locked=True)
    q.prime_path_access(t0 + 6, jobs=[hung.job], locked=True)
    old = q._PATH_PROBES[hung.k]["proc"]
    q.prime_path_access(t0 + q.PATH_PROBE_STALE_S + 1, jobs=[hung.job], locked=True)
    assert old.wait(5) is not None, "the stale probe was killed"
    q.prime_path_access(t0 + q.PATH_PROBE_STALE_S + 2, jobs=[hung.job], locked=True)
    assert q._PATH_PROBES[hung.k]["proc"] is not old and q._PATH_VERDICT[hung.k]["since"] == t0, \
        "reaped on the next prime and asked again"
    assert len(env.incidents("path-blocked")) == 1, "the same hold, not a new one"
    assert q._PATH_VERDICT[hung.k]["state"] == "blocked", "a killed probe gives no verdict (not err)"
    _kill_probes()
    q.prime_path_access(t0 + q.PATH_PROBE_STALE_S + 10, jobs=[], locked=True)
    [c] = env.incidents("path-cleared")
    assert c["then"] == "abandoned" and q._PATH_VERDICT == {}


def test_a_killed_probe_held_in_the_kernel_stays_tracked_until_it_exits(env, hung):
    """Fable review 2026-11-03: the stale kill dropped the probe at once, so a probe that a kill cannot end
    (it sleeps in the kernel until the access call returns) was joined by a second one and became a zombie."""
    t0, stale = 1_793_690_000.0, q.PATH_PROBE_STALE_S
    q.prime_path_access(t0, jobs=[hung.job], locked=True)
    q.prime_path_access(t0 + 6, jobs=[hung.job], locked=True)
    old = q._PATH_PROBES[hung.k]["proc"]
    real_kill, old.kill = old.kill, (lambda: None)      # the SIGKILL lands only when the kernel call returns
    q.prime_path_access(t0 + stale + 1, jobs=[hung.job], locked=True)
    q.prime_path_access(t0 + stale + 2, jobs=[hung.job], locked=True)
    assert list(q._PATH_PROBES) == [hung.k] and q._PATH_PROBES[hung.k]["proc"] is old, "no second probe joins it"
    assert q._PATH_PROBES[hung.k]["killed"] == t0 + stale + 1
    real_kill()
    old.wait(5)
    q.prime_path_access(t0 + stale + 3, jobs=[hung.job], locked=True)
    assert q._PATH_PROBES[hung.k]["proc"] is not old, "gone: asked again"
    assert q._PATH_VERDICT[hung.k]["state"] == "blocked" and q._PATH_VERDICT[hung.k]["since"] == t0
    assert len(env.incidents("path-blocked")) == 1 and env.incidents("path-cleared") == []


def test_the_gate_fails_open(env, hung, monkeypatch, capsys):
    t0 = 1_793_690_000.0
    q.prime_path_access(t0, jobs=[hung.job], locked=True)
    q.prime_path_access(t0 + 6, jobs=[hung.job], locked=True)
    assert q.path_access_wait(hung.job, t0 + 6) is not None
    capsys.readouterr()
    late = t0 + 6 + q.PATH_PRIME_STALE_S + 1
    assert q.path_access_wait(hung.job, late) is None, "no recent prime: launch"
    assert q.path_access_wait(hung.job, late + 5) is None
    err = capsys.readouterr().err
    assert err.count("path-access gate fail-open") == 1 and "last prime 121s old" in err, "said once, not per job"
    monkeypatch.setattr(q, "PATH_ACCESS_GATE", False)
    assert q.path_access_wait(hung.job, t0 + 6) is None and q.path_blocked_list() == []
    assert q.prime_path_access(t0 + 7, jobs=[hung.job], locked=True) == {}
    monkeypatch.setattr(q, "PATH_ACCESS_GATE", True)
    monkeypatch.setattr(q, "_STATE_LOCK_DEPTH", 1)
    assert q.prime_path_access(t0 + 8, jobs=[hung.job], locked=True) == {}, "never under the state lock"


def test_a_verdict_taken_unlocked_is_asked_again_once_the_screen_locks():
    now = 1000.0
    v = {"state": "ok", "at": now - 10, "locked": False}
    assert q._path_verdict_fresh(v, now, False) and q._path_verdict_fresh(v, now, None)
    assert not q._path_verdict_fresh(v, now, True)
    assert q._path_verdict_fresh(dict(v, locked=True), now, True)
    assert not q._path_verdict_fresh(dict(v, state="blocked"), now, False)
    assert not q._path_verdict_fresh(dict(v, at=now - q.PATH_PROBE_OK_TTL_S - 1), now, False)


def test_the_prime_reads_the_queue_state_without_the_lock(env, monkeypatch):
    st = env.tmp / "state.json"
    d = env.tmp / "w"
    d.mkdir()
    st.write_text(json.dumps({"jobs": [{"id": "a", "status": "pending", "cwd": str(d)}]}))
    monkeypatch.setattr(q, "STATE_PATH", st)
    assert q.prime_path_access(time.time(), locked=False) == {("path", str(d)): "ok"}
    st.write_text("{torn")
    assert q.prime_path_access(time.time(), locked=False) == {}, "a torn read is skipped, never raised"


# ---------------------------------------------------------------- the janitor closes the INFRA row; the deadman waits
J = _load("janitor_pathgate", "escalation_index_janitor.py")
ROW = "- [ ] `path-access` **INFRA** Q - 2026-11-03T06:00:00Z - needs the owner: queued jobs wait: a new process ..."


def facts(tmp, state):
    qs = tmp / "q.json"
    if state is not None:
        qs.write_text(json.dumps(state))
    return J.Facts(runs_dir=tmp / "runs", plans_dir=tmp / "runs", queue_state=qs, cancelled=lambda p: False,
                   bg_config={"mode": "off"})


def test_janitor_closes_the_path_access_row_on_evidence_only(env):
    [(_off, _line, row)] = J.scan(ROW + "\n")
    assert (row["plan"], row["who"].strip(), row["src"]) == ("path-access", "INFRA", "Q")
    close, why = J.decide(row, facts(env.tmp, {"jobs": [], "_path_blocked": ["/Users/p/Desktop/r/.git/HEAD"]}))
    assert close is False and why == "still held: /Users/p/Desktop/r/.git/HEAD"
    assert J.decide(row, facts(env.tmp, {"jobs": []}))[0] is True
    assert J.decide(row, facts(env.tmp, {"jobs": [], "_path_blocked": []}))[0] is True
    (env.tmp / "q.json").unlink()
    assert J.decide(row, facts(env.tmp, None)) == (False, "queue state unreadable")
    assert J.decide(dict(row, plan="other"), facts(env.tmp, {"jobs": []})) == (False, "unknown INFRA key other")


D = _load("deadman_pathgate", "queue-deadman.py")


def deadman(code, minutes):
    a = SimpleNamespace(stall_min=15, hb_max=300)
    mem, t0 = {}, 1_793_690_000.0
    out = None
    for m in (0, minutes):
        now = t0 + m * 60
        snap = {"state": {"jobs": [{"id": "j1", "status": "pending"}]}, "bloom": {}, "health_ok": None,
                "wait": {"updated_at": now, "jobs": {"j1": {"code": code, "short": "x"}}}}
        out = D.evaluate(snap, mem, now, a)
    return out


def test_deadman_lets_a_sandbox_hold_or_a_locked_screen_run_long():
    assert deadman("darkbloom-sandbox", 20) == {}
    assert "stalled:darkbloom-sandbox" in deadman("darkbloom-sandbox", 6 * 60 + 1)
    assert deadman("infra-path-access", 6 * 60 + 1) == {}
    assert "stalled:infra-path-access" in deadman("infra-path-access", 12 * 60 + 1)
    assert "stalled:lane-busy" in deadman("lane-busy", 16)
