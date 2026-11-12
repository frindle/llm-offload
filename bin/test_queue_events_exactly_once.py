#!/usr/bin/env python3
"""Exactly-once simulation of the job-notification path
(queue sidecars -> queue-event-log.py -> queue-events.jsonl -> queue-events-deliver.py -> session).

Property: every terminal event (done / passed / failed / blocked / needs_opus / cancelled / needs_info)
of a job reaches the session that launched it exactly once: no drop, no duplicate, never another session.
An event nobody owns (no launcher, launcher socket gone) reaches one session, once.

Hermetic: each test builds its own world under pytest's tmp_path (QEL_DIR / QEL_LOG_DIR / QEL_STATE_PATH /
QEL_STATUS_CMD overrides, fake session sockets that are plain files, no queue daemon, no HTTP, nothing
under ~/.ollama-dispatch). Modules are loaded from their file paths, subprocesses get an allow-listed
environment and a timeout, and every spawned process is killed at teardown.

Run (as a script or with pytest; EXACTLY_ONCE_ROUNDS=N multiplies the fuzz seeds and stress runs):
    python3 -m pytest -p no:cacheprovider -q -s test_queue_events_exactly_once.py

Design caveats pinned here (xfail(strict=True) = a property the design does not give today; the test
flips to a failure if someone starts giving it, so the entry must then be removed):
  * advance-then-print: cursors move before the output is written, so a deliverer SIGKILLed in between
    loses that batch (at-most-once at the output boundary);
  * the cursor lock is best effort: a holder wedged longer than QEW_LOCK_WAIT_S makes the next deliverer
    proceed unlocked, so a line can then be shown twice (at-least-once; the lines are identical, so a
    consumer can dedupe on "#<seq>");
  * session liveness is "its socket file exists": a SIGKILLed session leaves a stale socket, and its
    events are then treated as a live peer's and shown to nobody;
  * one global cursor (a single high-water mark): a live peer that looks at another live session's event
    advances past it, so if that owner exits before it looked and an unowned event then moved the global
    cursor past its seq, no session is ever shown it;
  * one writer: concurrent writers replay records (same seq, harmless) and can die on the shared state
    tmp name; the log keeps a single rotated generation, so a reader that lags two rotations loses data.
"""
import collections
import importlib.util
import json
import os
import random
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent
WRITER, DELIVER = BIN / "queue-event-log.py", BIN / "queue-events-deliver.py"
PY = sys.executable
ROUNDS = max(1, int(os.environ.get("EXACTLY_ONCE_ROUNDS", "1")))

# terminal kind -> (sidecar status, runner verdict); needs_info has no sidecar (status row + live state)
KINDS = {"passed": ("done", "PASS (all green)"), "done": ("done", None), "failed": ("failed", None),
         "blocked": ("blocked", None), "needs_opus": ("needs_opus", None), "cancelled": ("cancelled", None)}
EVENT_LINE = re.compile(r"^(?:queue )?#(\d+) (\S+) \(([^)]*)\): (\S+) \[([^\]]*)\]")


# ------------------------------------------------------------------------------------------ helpers
def parse_line(ln):
    m = EVENT_LINE.match(ln)
    return {"seq": int(m[1]), "state": m[2], "tag": m[3], "label": m[4], "id": m[5]} if m else None


def hook_lines(stdout):
    """The event/digest lines a hook printed (line 0 of additionalContext is the instruction header)."""
    if not stdout.strip():
        return []
    return json.loads(stdout)["hookSpecificOutput"]["additionalContext"].split("\n")[1:]


def ids(lines):
    return sorted(e["id"] for e in map(parse_line, lines) if e)


def tags(lines):
    return {e["id"]: e["tag"] for e in map(parse_line, lines) if e}


def wait_for(cond, timeout, what, step=0.03):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(step)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def atomic_write(path, text):
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    Path(tmp).write_text(text)
    os.replace(tmp, path)


def load(path, env):
    """Import a script by file path with `env` overlaid on os.environ (the modules read it at import)."""
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location("qex_" + uuid.uuid4().hex[:8], str(path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return mod


def rec(seq, jid, state="failed", owner=None, **extra):
    r = {"seq": seq, "ts": "2026-11-02T00:00:00+0000", "id": jid, "label": "job-" + jid, "state": state}
    if owner:
        r["launched_by"] = "uds:" + owner
    r.update(extra)
    return r


def put(path, *recs, mode="a"):
    with open(path, mode) as f:
        f.write("".join(json.dumps(r) + "\n" for r in recs))


class Sim:
    """One hermetic world: dirs, fake session sockets, the writer and the deliverer loaded in-process,
    plus helpers to run either as the real subprocess."""

    def __init__(self, root, **env):
        self.root = Path(root)
        self.d, self.logs, self.socks = (self.root / n for n in ("d", "logs", "socks"))
        for p in (self.d, self.logs, self.socks):
            p.mkdir(parents=True)
        self.status, self.state = self.root / "status.txt", self.root / "state.json"
        self.status.write_text("")
        self.state.write_text('{"jobs": []}')
        self.env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL") if k in os.environ}
        self.env.update(PYTHONDONTWRITEBYTECODE="1", QEL_DIR=str(self.d), QEL_LOG_DIR=str(self.logs),
                        QEL_STATE_PATH=str(self.state), QEL_STATUS_CMD="cat " + shlex.quote(str(self.status)))
        self.env.update({k: str(v) for k, v in env.items()})
        self.W, self.D = load(WRITER, self.env), load(DELIVER, self.env)
        for m in (self.W, self.D):   # nothing may point at the real queue
            assert (m.DIR, m.LOG_DIR, m.STATE_PATH) == (str(self.d), str(self.logs), str(self.state))
        self.paused = {}

    # -- the world
    @property
    def log(self):
        return self.d / "queue-events.jsonl"

    def sock(self, pid, alive=True):
        """Fake messaging socket <socks>/<pid>.sock: a plain file, since liveness is 'the file exists'."""
        p = self.socks / f"{pid}.sock"
        if alive:
            p.touch()
        return str(p)

    def job(self, jid, kind, owner=None, session=None):
        """The queue's <id>.done.json sidecar for a terminal job, written atomically (tmp + replace)."""
        status, verdict = KINDS[kind]
        r = {"id": jid, "label": "job-" + jid, "status": status}
        if verdict:
            r["runner_verdict"] = verdict
        if owner:
            r["launched_by"] = "uds:" + owner
        if session:
            r["launched_by_session"] = session
        atomic_write(self.logs / f"{jid}.done.json", json.dumps(r))

    def ask(self, jid, owner=None):
        """A job parked on a question: needs_info = a paused status row + the live state row."""
        self.paused[jid] = owner
        rows = [dict({"id": j, "status": "paused", "pause_reason": "needs_info", "label": "job-" + j},
                     **({"launched_by": "uds:" + o} if o else {})) for j, o in self.paused.items()]
        atomic_write(self.state, json.dumps({"jobs": rows}))
        atomic_write(self.status, "".join(f"[paused  ] {j}  job-{j}\n" for j in self.paused))

    def poll(self):
        return self.W.poll_once()

    def burst(self, owner, k, tag, kind="failed"):
        got = [f"{tag}{i}" for i in range(k)]
        for jid in got:
            self.job(jid, kind, owner)
        self.poll()
        return got

    def rotating(self, fn):
        """fn() with the size cap at 1 byte, so the writer's next append rotates the log first."""
        old, self.W.MAX_BYTES = self.W.MAX_BYTES, 1
        try:
            return fn()
        finally:
            self.W.MAX_BYTES = old

    def rotate(self):
        os.replace(self.log, str(self.log) + ".1")

    def logged(self):
        """Every parseable record in the log files, oldest file first."""
        out = []
        for fn in ("queue-events.jsonl.1", "queue-events.jsonl"):
            try:
                out += self.D.parse_lines((self.d / fn).read_bytes())
            except OSError:
                pass
        return out

    # -- delivery
    def deliver(self, sock, sid, bridge=None, events=None):
        """In-process hook for the session owning `sock`: the lines it is shown now."""
        evs = self.D.read_events() if events is None else events
        return self.D.deliver(evs, sid, self.D.norm_sock(sock), bridge)[0]

    def denv(self, sock, **extra):
        e = dict(self.env)
        e["CLAUDE_CODE_MESSAGING_SOCKET"] = sock
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def hook(self, sock, sid, agent_id=None, timeout=60, **env):
        """The real hook process (stdin JSON -> hook JSON); returns the lines it printed."""
        inp = {"session_id": sid, "hook_event_name": "PostToolUse"}
        if agent_id:
            inp["agent_id"] = agent_id
        r = subprocess.run([PY, str(DELIVER)], env=self.denv(sock, **env), input=json.dumps(inp),
                           capture_output=True, text=True, timeout=timeout)
        assert r.returncode == 0, r.stderr[-800:]
        return hook_lines(r.stdout)


class Procs:
    def __init__(self):
        self.ps, self.files = [], []

    def popen(self, argv, out=None, **kw):
        if out is not None:
            for suffix, key in (("", "stdout"), (".err", "stderr")):
                f = open(str(out) + suffix, "w")
                self.files.append(f)
                kw.setdefault(key, f)
        kw.setdefault("stderr", subprocess.DEVNULL)
        p = subprocess.Popen(argv, **kw)
        self.ps.append(p)
        return p

    def close(self):
        for p in self.ps:
            if p.poll() is None:
                p.kill()
        for p in self.ps:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        for f in self.files:
            f.close()


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("CLAUDE_CODE_", "QEL_", "QEW_")) or k == "CLAUDE_PID":
            monkeypatch.delenv(k)


@pytest.fixture
def procs():
    p = Procs()
    yield p
    p.close()


def script(tmp, name, body):
    p = Path(tmp) / name
    p.write_text(body)
    return str(p)


class Ledger:
    """What each session was shown, by which deliverer. Thread-safe."""

    def __init__(self):
        self.rows, self._lock = [], threading.Lock()   # (who, via, seq, state, tag, id)

    def add(self, who, via, lines):
        got = [e for e in map(parse_line, lines) if e]
        with self._lock:
            self.rows += [(who, via, e["seq"], e["state"], e["tag"], e["id"]) for e in got]

    def count(self):
        with self._lock:
            return collections.Counter(r[5] for r in self.rows)


def audit(rows, jobs):
    """Problems between what was shown and what must have been. jobs = {id: (state, owner or None, tag)}:
    exactly once, in that state and tag, to the owner when there is one (to anyone when not)."""
    got = collections.defaultdict(list)
    for who, via, seq, state, tag, jid in rows:
        got[jid].append((who, via, seq, state, tag))
    bad = []
    for jid, (state, owner, tag) in jobs.items():
        shown = got.pop(jid, [])
        if len(shown) != 1:
            bad.append(f"{jid} [{state}, owner={owner}] shown {len(shown)}x: {shown}")
            continue
        who, via, seq, st, tg = shown[0]
        if st != state or tg != tag:
            bad.append(f"{jid} shown as {st}/{tg}, expected {state}/{tag}")
        if owner is not None and who != owner:
            bad.append(f"{jid} [owner={owner}] leaked to {who}")
    bad += [f"unexpected delivery {jid}: {s}" for jid, s in got.items()]
    return bad


# ---------------------------------------------------------------------- 1. concurrency under load
PRODUCER = r'''
import json, os, sys, time
plan, logs = json.load(open(sys.argv[1])), sys.argv[2]
t0 = time.time()
for t, jid, status, verdict, owner in sorted(plan):
    time.sleep(max(0.0, t0 + t - time.time()))
    r = {"id": jid, "label": "job-" + jid, "status": status}
    if verdict:
        r["runner_verdict"] = verdict
    if owner:
        r["launched_by"] = "uds:" + owner
    tmp = os.path.join(logs, "%s.done.json.%d.tmp" % (jid, os.getpid()))
    open(tmp, "w").write(json.dumps(r))
    os.replace(tmp, os.path.join(logs, jid + ".done.json"))
'''

STRESS_RUNS = [(11, False), (12, True)] + [(100 + i, bool(i % 2)) for i in range(ROUNDS - 1)]


@pytest.mark.parametrize("seed,rotate", STRESS_RUNS, ids=[f"seed{s}-{'rot' if r else 'norot'}" for s, r in STRESS_RUNS])
def test_stress_producers_writer_daemon_hooks_and_watchers(tmp_path, procs, seed, rotate):
    """3 sidecar-producer processes + the real writer daemon + a hook loop per session + a watcher for two
    of them, 4 live sessions, unowned and launcher-gone events, all seven states, (rot) one log rotation."""
    rng = random.Random(seed)
    S, PER, UNOWNED, ORPHAN, SPAN = 4, 28, 24, 16, 3.0
    total = S * PER + UNOWNED + ORPHAN
    extra = {}
    if rotate:   # cap at ~60% of the volume: exactly one rotation, so the single kept generation never overflows
        cal = Sim(tmp_path / "cal")
        cal.poll()
        cal.job("c0", "failed", cal.sock(1100))
        cal.poll()
        extra["QEL_MAX_BYTES"] = int(cal.log.stat().st_size * total * 0.6)
    sim = Sim(tmp_path / "w", QEL_INTERVAL=0.05, QEW_POLL_S=0.05, QEW_LOCK_WAIT_S=30, QEW_IDLE_EXIT_S=600, **extra)
    socks = [sim.sock(1100 + i) for i in range(S)]
    ghosts = [str(sim.socks / f"{1900 + i}.sock") for i in range(3)]   # launchers that are gone
    kinds, jobs, plan, asks, n = list(KINDS), {}, [], [], 0

    def new_id():
        nonlocal n
        n += 1
        return f"{seed}x{n:04d}"

    for i in range(S):
        for k in range(PER):
            jid = new_id()
            if k % 7 == 6:   # 4 per session park on a question
                jobs[jid] = ("needs_info", f"s{i}", "yours")
                asks.append((rng.uniform(0, SPAN), jid, socks[i]))
            else:
                kind = kinds[(k + i) % len(kinds)]
                jobs[jid] = (kind, f"s{i}", "yours")
                plan.append((jid, kind, socks[i]))
    for k in range(UNOWNED):
        jid, kind = new_id(), kinds[k % len(kinds)]
        jobs[jid] = (kind, None, "no launcher")
        plan.append((jid, kind, None))
    for k in range(ORPHAN):
        jid, kind = new_id(), kinds[k % len(kinds)]
        jobs[jid] = (kind, None, "launcher gone")
        plan.append((jid, kind, ghosts[k % 3]))
    rng.shuffle(plan)
    assert len(jobs) == total and {v[0] for v in jobs.values()} == set(KINDS) | {"needs_info"}

    writer = procs.popen([PY, str(WRITER)], out=tmp_path / "writer.out", env=sim.env)
    wait_for(lambda: (sim.d / "queue-events.state.json").exists(), 30, "the writer's baseline poll")
    for i in range(S):   # the UserPromptSubmit hook: every session has cursors before any job exists
        assert sim.hook(socks[i], f"sess{i}") == []

    ledger, errors, stop, calls = Ledger(), [], threading.Event(), collections.Counter()

    def hook_loop(i):
        r = random.Random(seed * 100 + i)
        try:
            while not stop.is_set():
                ledger.add(f"s{i}", "hook", sim.hook(socks[i], f"sess{i}"))
                calls["hook"] += 1
                time.sleep(r.uniform(0, 0.03))
        except BaseException as e:   # surfaced by the main thread
            errors.append(e)

    threads = [threading.Thread(target=hook_loop, args=(i,), daemon=True) for i in range(S)]
    watch = {}
    for i in (0, 1):   # s0's watcher shares its session cursor; s1's has only the process cursor
        env = sim.denv(socks[i], **({"CLAUDE_CODE_SESSION_ID": "sess0"} if i == 0 else {}))
        procs.popen([PY, str(DELIVER), "--watch"], out=tmp_path / f"watch{i}.out", env=env)
        watch[i] = [tmp_path / f"watch{i}.out", 0]

    def pump():
        for i, w in watch.items():
            data = w[0].read_bytes()
            nl = data.rfind(b"\n")
            if nl >= w[1]:
                ledger.add(f"s{i}", "watch", data[w[1]:nl + 1].decode().splitlines())
                w[1] = nl + 1

    def feeder():
        t0 = time.time()
        try:
            for t, jid, owner in sorted(asks):
                time.sleep(max(0.0, t0 + t - time.time()))
                sim.ask(jid, owner)
        except BaseException as e:
            errors.append(e)

    slices = []
    for p in range(3):
        rows = [[rng.uniform(0, SPAN), jid, KINDS[kind][0], KINDS[kind][1], owner] for jid, kind, owner in plan[p::3]]
        slices.append(script(tmp_path, f"plan{p}.json", json.dumps(rows)))
    driver = script(tmp_path, "producer.py", PRODUCER)
    for t in threads:
        t.start()
    feed = threading.Thread(target=feeder, daemon=True)
    feed.start()
    producers = [procs.popen([PY, driver, s, str(sim.logs)], out=tmp_path / f"producer{p}.out")
                 for p, s in enumerate(slices)]
    for p in producers:
        assert p.wait(timeout=60) == 0
    feed.join(timeout=30)

    def shown_all():
        assert not errors, errors
        pump()
        return len(ledger.count()) >= total

    wait_for(lambda: len({e["id"] for e in sim.logged()}) == total, 90, "the writer to log every event")
    wait_for(shown_all, 90, "every event to be shown to a session")
    time.sleep(1.0)   # a duplicate would surface in this window
    stop.set()
    for t in threads:
        t.join(timeout=60)
    pump()
    for p in procs.ps:   # watchers and the writer daemon
        if p.poll() is None:
            p.terminate()
    for p in procs.ps:
        p.wait(timeout=15)
    pump()
    assert not errors, errors
    final = {i: sim.hook(socks[i], f"sess{i}") for i in range(S)}   # nothing is left to show anyone
    assert final == {i: [] for i in range(S)}
    logged = sim.logged()
    assert len(logged) == total, f"{len(logged)} log records for {total} events"
    if rotate:
        assert (sim.d / "queue-events.jsonl.1").exists(), "calibration: the log never rotated"
    problems = audit(ledger.rows, jobs)
    assert not problems, "\n".join(problems[:25])
    via = collections.Counter(r[1] for r in ledger.rows)
    print(f"\n[stress seed={seed} rotate={rotate}] events={total} states={len({v[0] for v in jobs.values()})} "
          f"sessions={S} hook_calls={calls['hook']} shown_by_hook={via['hook']} shown_by_watch={via['watch']} "
          f"log_records={len(logged)} rotated={(sim.d / 'queue-events.jsonl.1').exists()}")


GO_HOOK = r'''
import os, runpy, sys, time
go, deliver, inp = sys.argv[1:4]
sys.stdin = open(inp)
while not os.path.exists(go):
    time.sleep(0.0005)
sys.argv = [deliver]
runpy.run_path(deliver, run_name="__main__")
'''


def test_hooks_and_a_watcher_race_for_one_session_while_events_stream_in(tmp_path, procs):
    """H hook processes released at one instant, round after round, plus a watcher on the same session,
    while the writer is still appending. Each event is shown once, by exactly one of them, to A only;
    a live peer's event and the unowned ones ride along."""
    R, H = 14, 4
    sim = Sim(tmp_path / "w", QEW_POLL_S=0.01, QEW_LOCK_WAIT_S=30, QEW_IDLE_EXIT_S=600)
    A, B = sim.sock(1201), sim.sock(1202)   # B is a live peer that never looks: its events are not A's
    sim.poll()
    assert sim.hook(A, "a1") == []
    watch_out = tmp_path / "watch.out"
    watcher = procs.popen([PY, str(DELIVER), "--watch"], out=watch_out,
                          env=sim.denv(A, CLAUDE_CODE_SESSION_ID="a1"))
    driver = script(tmp_path, "go_hook.py", GO_HOOK)
    inp = script(tmp_path, "hook.json", json.dumps({"session_id": "a1", "hook_event_name": "PostToolUse"}))
    ledger, jobs, kinds, pos, n = Ledger(), {}, list(KINDS), [0], [0]

    def pump_watch():
        data = watch_out.read_bytes()
        nl = data.rfind(b"\n")
        if nl >= pos[0]:
            ledger.add("A", "watch", data[pos[0]:nl + 1].decode().splitlines())
            pos[0] = nl + 1

    def mk(owner):
        n[0] += 1
        jid, kind = f"r{n[0]:04d}", kinds[n[0] % len(kinds)]
        sim.job(jid, kind, owner)
        if owner != B:
            jobs[jid] = (kind, "A" if owner else None, "yours" if owner else "no launcher")

    for rnd in range(R):
        for owner in (A, A, B, None):
            mk(owner)
        sim.poll()
        go = tmp_path / f"go{rnd}"
        ps = [procs.popen([PY, driver, str(go), str(DELIVER), inp], out=tmp_path / f"h{rnd}_{j}.out",
                          env=sim.denv(A)) for j in range(H)]
        time.sleep(0.15)   # let them all reach the spin loop
        go.touch()
        for _ in range(3):   # the writer keeps appending while the deliverers run
            mk(A)
            sim.poll()
            time.sleep(0.003)
        for j, p in enumerate(ps):
            assert p.wait(timeout=60) == 0
            ledger.add("A", "hook", hook_lines((tmp_path / f"h{rnd}_{j}.out").read_text()))
        pump_watch()
    wait_for(lambda: (pump_watch(), len(ledger.count()) >= len(jobs))[1], 30, "every event to be shown")
    time.sleep(0.8)   # a duplicate would surface in this window
    pump_watch()
    watcher.terminate()
    watcher.wait(timeout=15)
    problems = audit(ledger.rows, jobs)
    assert not problems, "\n".join(problems[:20])
    assert sim.hook(A, "a1") == []
    via = collections.Counter(r[1] for r in ledger.rows)
    print(f"\n[race] rounds={R} hooks_per_round={H} events={len(jobs)} by_hook={via['hook']} by_watch={via['watch']}")


def test_deliverers_queued_on_the_cursor_lock_share_one_delivery(tmp_path, procs):
    """Deterministic contention: one deliverer holds the cursor lock (read the events, no cursor written
    yet) while two more hooks and a watcher for the same session queue behind it. When it finishes, the
    events were shown once, by the holder; the three that waited find the cursors advanced."""
    sim, A, B = kill_scene(tmp_path)
    holder, release = start_driver(sim, procs, tmp_path, A, "a", "wr0")
    inp = script(tmp_path, "hook.json", json.dumps({"session_id": "a", "hook_event_name": "PostToolUse"}))
    waiters = [procs.popen([PY, str(DELIVER)], out=tmp_path / f"q{i}.out", env=sim.denv(A), stdin=open(inp))
               for i in range(2)]
    watcher = procs.popen([PY, str(DELIVER), "--watch"], out=tmp_path / "qw.out",
                          env=sim.denv(A, CLAUDE_CODE_SESSION_ID="a", QEW_POLL_S=0.02, QEW_IDLE_EXIT_S=600))
    time.sleep(1.0)
    assert all(p.poll() is None for p in waiters), "a hook got through while the lock was held"
    release.touch()
    assert holder.wait(timeout=30) == 0
    for p in waiters:
        assert p.wait(timeout=30) == 0
    time.sleep(0.6)   # the watcher's turn on the lock and a few polls
    watcher.terminate()
    watcher.wait(timeout=15)
    assert ids(hook_lines((tmp_path / "wr0.out").read_text())) == ["ka1", "ka2", "ka3", "ku1", "ku2"]
    assert [(tmp_path / f"q{i}.out").read_text() for i in range(2)] == ["", ""]
    assert (tmp_path / "qw.out").read_text() == ""
    assert sim.hook(A, "a") == [] and ids(sim.hook(B, "b")) == ["kb1", "kb2"]


@pytest.mark.parametrize("seed", range(6 * ROUNDS))
def test_interleaved_sessions_fuzz(tmp_path, seed):
    """Seeded interleaving of job creation, writer polls, hooks, /clear, session death and birth.
    Owner alive at the end: its events reach it exactly once and nobody else. Unowned / launcher-gone
    events reach exactly one session. An owner that died: at most once, never to a peer while it lived."""
    rng = random.Random(seed)
    sim = Sim(tmp_path / "w")
    sim.poll()
    sess, jobs, shown, step = {}, {}, collections.defaultdict(list), [0]
    pids, kinds, ghost, nj = iter(range(2100, 3000)), list(KINDS), str(sim.socks / "9999.sock"), [0]

    def look(name):
        s = sess[name]
        for e in map(parse_line, sim.deliver(s["sock"], s["sid"])):
            shown[e["id"]].append((name, e["seq"], step[0]))

    def spawn():
        pid = next(pids)
        name = f"s{pid}"
        sess[name] = {"sock": sim.sock(pid), "sid": f"{name}-0", "alive": True, "died": None, "clears": 0}
        look(name)   # the first hook of a session: cursors exist before its jobs do

    for _ in range(3):
        spawn()
    ops = 420
    for i in range(ops):
        step[0] = i
        alive = [k for k, v in sess.items() if v["alive"]]
        r = rng.random()
        if r < 0.30:
            nj[0] += 1
            jid, kind, w = f"f{nj[0]:04d}", rng.choice(kinds), rng.random()
            if alive and w < 0.6:
                o = rng.choice(alive)
                sim.job(jid, kind, sess[o]["sock"])
                jobs[jid] = {"cls": "owned", "owner": o}
            elif w < 0.8:
                sim.job(jid, kind, None)
                jobs[jid] = {"cls": "unowned"}
            else:
                sim.job(jid, kind, ghost)
                jobs[jid] = {"cls": "gone"}
        elif r < 0.45:
            sim.poll()
        elif r < 0.80 and alive:
            look(rng.choice(alive))
        elif r < 0.86 and alive:   # /clear: new session id, same process
            s = sess[rng.choice(alive)]
            s["clears"] += 1
            s["sid"] = s["sid"].rsplit("-", 1)[0] + f"-{s['clears']}"
        elif r < 0.90 and len(alive) > 1:   # a session exits: its socket goes
            name = rng.choice(alive)
            os.unlink(sess[name]["sock"])
            sess[name].update(alive=False, died=i)
        elif r < 0.95:
            spawn()
    step[0] = ops
    sim.poll()
    alive = [k for k, v in sess.items() if v["alive"]]
    if not alive:
        spawn()
        alive = [k for k, v in sess.items() if v["alive"]]
    for name in alive:
        look(name)
    before = {k: list(v) for k, v in shown.items()}
    for name in alive:
        look(name)
    assert shown == before, "a settled session was shown something again"
    assert len(jobs) >= 60
    bad, lost = [], 0
    for jid, j in jobs.items():
        got = shown.get(jid, [])
        if j["cls"] != "owned":
            if len(got) != 1:
                bad.append(f"{jid} ({j['cls']}) shown {len(got)}x {got}")
            continue
        o = sess[j["owner"]]
        if o["alive"]:
            if [g[0] for g in got] != [j["owner"]]:
                bad.append(f"{jid} owner {j['owner']} alive, shown {got}")
        else:   # the owner exited: its event reached it, or was handed to one other session, never both / twice
            lost += not got   # a design gap (see the xfail test below), counted not asserted
            if len(got) > 1:
                bad.append(f"{jid} owner {j['owner']} died, shown {len(got)}x {got}")
            bad += [f"{jid} leaked to {w} while owner {j['owner']} lived" for w, _, st in got
                    if w != j["owner"] and st <= o["died"]]
    assert not bad, "\n".join(bad[:20])
    n_owned = sum(1 for j in jobs.values() if j["cls"] == "owned")
    print(f"\n[fuzz seed={seed}] ops={ops} events={len(jobs)} owned={n_owned} sessions={len(sess)} "
          f"died={sum(1 for v in sess.values() if not v['alive'])} shown={sum(len(v) for v in shown.values())} "
          f"owner_died_undelivered_and_skipped={lost}")


def test_concurrent_writer_processes_replay_records_that_are_delivered_once(tmp_path, procs):
    """Outside the supported topology (one writer: the LaunchAgent) but cheap to pin. Three writers that
    all see the same new sidecars append the same records (same seq) up to three times, and may die on
    the shared state tmp name. Nothing is dropped by the writers, and delivery shows each event once."""
    logged = shown = 0
    for rnd in range(4):
        sim = Sim(tmp_path / f"w{rnd}")
        A = sim.sock(2201)
        sim.poll()
        assert sim.hook(A, "a") == []
        want = {}
        for k in range(10):
            kind = list(KINDS)[k % len(KINDS)]
            sim.job(f"w{k}", kind, A if k % 3 else None)
            want[f"w{k}"] = kind
        ps = [procs.popen([PY, str(WRITER), "--once"], out=tmp_path / f"wr{rnd}_{j}.out", env=sim.env)
              for j in range(3)]
        for p in ps:
            p.wait(timeout=60)   # rc 1 is tolerated: the shared tmp name
        in_log = collections.Counter(e["id"] for e in sim.logged())
        assert set(in_log) == set(want), "a writer dropped a record"
        lines = sim.hook(A, "a")
        assert ids(lines) == sorted(want)
        assert sim.hook(A, "a") == []
        logged, shown = logged + sum(in_log.values()), shown + len(lines)
    print(f"\n[concurrent writers] rounds=4 events=40 log_records={logged} shown={shown}")


# -------------------------------------------------------------------------- 2. kills and crashes
HOOK_DRIVER = r'''
import importlib.util, io, os, sys, time
path, point, marker, release = sys.argv[1:5]
spec = importlib.util.spec_from_file_location("qed_drv", path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
def block():
    open(marker, "w").write(point)
    end = time.time() + 120
    while time.time() < end and not os.path.exists(release):
        time.sleep(0.01)
n, orig = [0], m.wr
def wr(p, t):
    if point == "wr%d" % n[0]:
        block()
    n[0] += 1
    return orig(p, t)
m.wr = wr
if point == "print":
    real = sys.stdout
    class Out:
        def write(self, s):
            block()
            return real.write(s)
        def flush(self):
            real.flush()
    sys.stdout = Out()
sys.stdin = io.StringIO(os.environ["DRV_STDIN"])
m.hook()
sys.stdout.flush()
'''

WRITER_DRIVER = r'''
import importlib.util, os, sys, time
path, marker = sys.argv[1:3]
spec = importlib.util.spec_from_file_location("qel_drv", path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
orig = m.write_atomic
def write_atomic(p, data):
    if p.endswith("queue-events.state.json"):   # the log append is done; the state save is next
        open(marker, "w").write("x")
        time.sleep(120)
    return orig(p, data)
m.write_atomic = write_atomic
m.poll_once()
'''


def kill_scene(tmp_path):
    """Two live sessions, A (3 jobs) and B (2 jobs), plus two unowned events; cursors primed; events logged."""
    sim = Sim(tmp_path / "w", QEW_LOCK_WAIT_S=30)
    A, B = sim.sock(1301), sim.sock(1302)
    sim.poll()
    assert sim.hook(A, "a") == [] and sim.hook(B, "b") == []
    for jid, kind, o in (("ka1", "failed", A), ("ka2", "passed", A), ("ka3", "needs_opus", A),
                         ("kb1", "blocked", B), ("kb2", "cancelled", B),
                         ("ku1", "done", None), ("ku2", "failed", None)):
        sim.job(jid, kind, o)
    sim.poll()
    return sim, A, B


def start_driver(sim, procs, tmp, sock, sid, point):
    marker, release = tmp / f"{point}.marker", tmp / f"{point}.release"
    env = sim.denv(sock, DRV_STDIN=json.dumps({"session_id": sid, "hook_event_name": "PostToolUse"}))
    p = procs.popen([PY, script(tmp, "hook_driver.py", HOOK_DRIVER), str(DELIVER), point, str(marker), str(release)],
                    out=tmp / f"{point}.out", env=env)
    wait_for(lambda: marker.exists() or p.poll() is not None, 30, f"the driver to reach {point}")
    assert marker.exists(), (tmp / f"{point}.out.err").read_text()[-800:]
    return p, release


def test_sigkill_between_reading_and_advancing_loses_nothing_and_frees_the_lock(tmp_path, procs):
    """A deliverer that read the events, holds the cursor lock and is SIGKILLed before any cursor write:
    a second deliverer queued on that lock gets it at once (no timeout) and shows everything, once."""
    sim, A, B = kill_scene(tmp_path)
    drv, _ = start_driver(sim, procs, tmp_path, A, "a", "wr0")
    inp = script(tmp_path, "hook.json", json.dumps({"session_id": "a", "hook_event_name": "PostToolUse"}))
    waiter = procs.popen([PY, str(DELIVER)], out=tmp_path / "waiter.out", env=sim.denv(A), stdin=open(inp))
    time.sleep(0.6)
    assert waiter.poll() is None, "the second deliverer should be queued on the held lock"
    drv.send_signal(signal.SIGKILL)
    drv.wait(timeout=10)
    t0 = time.time()
    assert waiter.wait(timeout=25) == 0
    assert time.time() - t0 < 10, "the SIGKILLed holder's flock was not released"
    lines = hook_lines((tmp_path / "waiter.out").read_text())
    assert ids(lines) == ["ka1", "ka2", "ka3", "ku1", "ku2"]
    assert sim.hook(A, "a") == []   # no duplicate after the restart
    assert ids(sim.hook(B, "b")) == ["kb1", "kb2"]   # and B still gets exactly its own


@pytest.mark.xfail(strict=True, reason="design: advance-then-print. Cursors move before the output is "
                   "written, so a deliverer SIGKILLed in between loses that batch (at-most-once there).")
@pytest.mark.parametrize("point", ["wr1", "print"])
def test_sigkill_after_cursor_advance_before_output_loses_events(tmp_path, procs, point):
    sim, A, B = kill_scene(tmp_path)
    drv, _ = start_driver(sim, procs, tmp_path, A, "a", point)
    drv.send_signal(signal.SIGKILL)
    drv.wait(timeout=10)
    assert (tmp_path / f"{point}.out").read_text() == ""   # the killed run printed nothing
    assert ids(sim.hook(A, "a")) == ["ka1", "ka2", "ka3", "ku1", "ku2"]


def test_lock_wait_timeout_fails_open_to_an_identical_duplicate_never_a_drop(tmp_path, procs):
    """Design: the cursor lock is best effort. A holder wedged past QEW_LOCK_WAIT_S lets the next deliverer
    proceed unlocked; when the holder resumes the batch is shown twice (at-least-once). The two copies are
    line-for-line identical, so a consumer dedupes on '#<seq>' and sees each event exactly once."""
    sim, A, B = kill_scene(tmp_path)
    drv, release = start_driver(sim, procs, tmp_path, A, "a", "wr0")   # holds the lock, cursors not yet written
    second = sim.hook(A, "a", QEW_LOCK_WAIT_S=0.3)   # times out on the lock, proceeds unlocked
    assert ids(second) == ["ka1", "ka2", "ka3", "ku1", "ku2"]
    release.touch()
    assert drv.wait(timeout=30) == 0
    first = hook_lines((tmp_path / "wr0.out").read_text())
    assert first == second, "the duplicate must be identical so '#<seq>' dedupes it"
    by_seq = {e["seq"]: e for e in map(parse_line, first + second)}
    assert sorted(e["id"] for e in by_seq.values()) == ["ka1", "ka2", "ka3", "ku1", "ku2"]
    assert sim.hook(A, "a") == []   # and it stops there


def test_watcher_sigkilled_between_batches_and_restarted_shows_each_event_once(tmp_path, procs):
    sim = Sim(tmp_path / "w", QEW_POLL_S=0.02, QEW_IDLE_EXIT_S=600, QEW_LOCK_WAIT_S=30)
    A = sim.sock(1401)
    sim.poll()
    assert sim.hook(A, "a") == []
    env = sim.denv(A, CLAUDE_CODE_SESSION_ID="a")

    def lines(path):
        return (tmp_path / path).read_text().splitlines()

    w1 = procs.popen([PY, str(DELIVER), "--watch"], out=tmp_path / "w1.out", env=env)
    first = sim.burst(A, 4, "p")
    wait_for(lambda: len(lines("w1.out")) >= 4, 30, "the first watcher to show batch 1")
    w1.send_signal(signal.SIGKILL)
    w1.wait(timeout=10)
    second = sim.burst(A, 4, "q")   # arrives while no watcher runs
    procs.popen([PY, str(DELIVER), "--watch"], out=tmp_path / "w2.out", env=env)
    wait_for(lambda: len(lines("w2.out")) >= 4, 30, "the restarted watcher to show batch 2")
    time.sleep(0.6)
    assert ids(lines("w1.out")) == sorted(first)
    assert ids(lines("w2.out")) == sorted(second)
    assert sim.hook(A, "a") == []


def test_writer_killed_after_append_before_state_save_replays_the_same_records(tmp_path, procs):
    """The writer appends, then saves its state. Killed in between, the restart re-emits the same records
    with the same seqs: the log holds each twice (at-least-once writer). Delivery shows each event once."""
    sim = Sim(tmp_path / "w")
    A = sim.sock(1501)
    sim.poll()
    assert sim.hook(A, "a") == []
    for jid, kind, o in (("w1", "failed", A), ("w2", "passed", A), ("w3", "blocked", None)):
        sim.job(jid, kind, o)
    marker = tmp_path / "writer.marker"
    drv = procs.popen([PY, script(tmp_path, "writer_driver.py", WRITER_DRIVER), str(WRITER), str(marker)],
                      out=tmp_path / "wdrv.out", env=sim.env)
    wait_for(lambda: marker.exists() or drv.poll() is not None, 30, "the writer to reach its state save")
    assert marker.exists()
    drv.send_signal(signal.SIGKILL)
    drv.wait(timeout=10)
    assert len(sim.logged()) == 3
    assert sim.poll() == 3   # restart: the same sidecars are still new
    keys = sorted((e["seq"], e["id"], e["state"]) for e in sim.logged())
    assert len(keys) == 6 and keys[0::2] == keys[1::2], keys
    assert ids(sim.hook(A, "a")) == ["w1", "w2", "w3"]   # one batch holding every record twice
    assert sim.hook(A, "a") == []
    assert ids(sim.deliver(A, "a-after-clear")) == []   # a /clear'd session resumes at the process cursor


def test_same_record_twice_in_one_batch_is_shown_once(tmp_path):
    sim = Sim(tmp_path / "w")
    A = sim.sock(1511)
    sim.poll()
    sim.deliver(A, "a")
    r1, r2, r3 = rec(1, "d1", owner=A), rec(2, "d2", "passed", owner=A), rec(3, "d3", "blocked")
    put(sim.log, r1, r2, r3, dict(r2, ts="later"), dict(r3, ts="later"), dict(r1, ts="later"))
    lines = sim.deliver(A, "a")
    assert [parse_line(ln)["seq"] for ln in lines] == [1, 2, 3], lines
    assert sim.deliver(A, "a") == []


def test_writer_appends_after_a_torn_tail_without_swallowing_the_next_record(tmp_path):
    """A writer killed mid-append leaves half a line. Its restart re-emits that record; appended straight
    after the fragment it would fuse with it into one unparseable line and the event would be lost."""
    sim = Sim(tmp_path / "w")
    A = sim.sock(1601)
    sim.poll()
    assert sim.hook(A, "a") == []
    sim.job("t1", "failed", A)
    sim.poll()
    state_before = (sim.d / "queue-events.state.json").read_bytes()
    sim.job("t2", "passed", A)
    sim.poll()
    (sim.d / "queue-events.state.json").write_bytes(state_before)   # the kill came before the state save
    raw = sim.log.read_bytes()
    sim.log.write_bytes(raw[:-40])                                  # ... and mid-append: t2 is cut short
    assert sim.poll() == 1   # restart re-emits t2
    assert sorted(e["id"] for e in sim.logged()) == ["t1", "t2"]
    assert ids(sim.hook(A, "a")) == ["t1", "t2"]
    assert sim.hook(A, "a") == []


# ------------------------------------------------------------------ 3. torn lines and log rotation
def test_partial_last_line_waits_for_its_newline_in_the_hook_and_the_tail(tmp_path):
    sim = Sim(tmp_path / "w")
    A = sim.sock(1701)
    sim.poll()
    sim.deliver(A, "a")
    full2 = json.dumps(rec(2, "p2", owner=A))
    put(sim.log, rec(1, "p1", owner=A))
    with open(sim.log, "a") as f:
        f.write(full2[:-12])                    # the writer is mid-append of record 2
    tail = sim.D.Tail()
    assert [e["seq"] for e in tail.read()] == [1]
    assert ids(sim.deliver(A, "a")) == ["p1"]
    assert tail.read() == [] and sim.deliver(A, "a") == []   # still torn: nothing, and nothing repeated
    with open(sim.log, "a") as f:
        f.write(full2[-12:] + "\n")
    assert [e["seq"] for e in tail.read()] == [2]
    assert ids(sim.deliver(A, "a")) == ["p2"]
    assert sim.deliver(A, "a") == []
    # a complete record that merely lacks its newline: the hook may show it, the tail waits for the newline,
    # and the cursor keeps the late tail read from showing it a second time
    with open(sim.log, "a") as f:
        f.write(json.dumps(rec(3, "p3", owner=A)))
    assert tail.read() == []
    assert ids(sim.deliver(A, "a")) == ["p3"]
    with open(sim.log, "a") as f:
        f.write("\n")
    late = tail.read()
    assert [e["seq"] for e in late] == [3] and sim.deliver(A, "a", events=late) == []


def test_tail_follows_rotation_and_truncation_without_drop_or_duplicate(tmp_path):
    sim = Sim(tmp_path / "w")
    A = sim.sock(1711)
    sim.poll()
    sim.deliver(A, "a")
    tail, shown = sim.D.Tail(), []

    def step():
        shown.extend(ids(sim.deliver(A, "a", events=tail.read())))

    put(sim.log, rec(1, "x1", owner=A), rec(2, "x2", owner=A))
    step()
    put(sim.log, rec(3, "x3", owner=A))      # unread when the log is rotated away
    sim.rotate()
    put(sim.log, rec(4, "x4", owner=A), rec(5, "x5", owner=A))
    step()                                   # finishes the rotated file, then follows the new one
    put(sim.log, rec(6, "x6", owner=A))
    step()
    sim.log.write_text("")                   # truncated in place (copytruncate style)
    put(sim.log, rec(7, "x7", owner=A))
    step()
    step()
    assert shown == ["x1", "x2", "x3", "x4", "x5", "x6", "x7"]
    assert sim.deliver(A, "a") == []         # the hook agrees with the tail


@pytest.mark.parametrize("older", [False, True], ids=["one-file", "with-older-rotated-file"])
def test_log_rotating_between_the_hooks_two_reads_loses_no_event(tmp_path, older):
    """The hook reads queue-events.jsonl.1 and queue-events.jsonl one after the other. A rotation between
    the two reads moves the unread tail into .1 after (or before) it was looked at: it must not vanish."""
    sim = Sim(tmp_path / "w")
    A = sim.sock(1801)
    sim.poll()
    sim.deliver(A, "a")
    want = []
    if older:
        want += sim.burst(A, 2, "o")
        sim.rotate()                          # .1 = the older generation
    want += sim.burst(A, 2, "x")              # in the current file, unread
    opened = []
    real_open = open

    def fake_open(path, *a, **k):
        if os.path.basename(str(path)).startswith("queue-events.jsonl"):
            opened.append(1)
            if len(opened) == 2:              # the writer rotates between the hook's first and second read
                want.extend(sim.rotating(lambda: sim.burst(A, 2, "y")))
        return real_open(path, *a, **k)

    sim.D.open = fake_open
    try:
        first = sim.deliver(A, "a")
    finally:
        del sim.D.open
    assert len(opened) >= 2 and (sim.d / "queue-events.jsonl.1").exists()
    second = sim.deliver(A, "a")
    assert sorted(ids(first) + ids(second)) == sorted(want)


def test_tail_rotation_between_its_stat_and_its_open_loses_no_event(tmp_path):
    sim = Sim(tmp_path / "w")
    A = sim.sock(1811)
    sim.poll()
    sim.deliver(A, "a")
    tail = sim.D.Tail()
    want = sim.burst(A, 2, "a")
    shown = ids(sim.deliver(A, "a", events=tail.read()))
    want += sim.burst(A, 2, "b")              # appended; the tail has not read it yet
    fired = []
    real_open = open

    def fake_open(path, *a, **k):
        if os.path.basename(str(path)) == "queue-events.jsonl" and not fired:
            fired.append(1)                   # stat said "same file, grown"; now the writer rotates + appends
            want.extend(sim.rotating(lambda: sim.burst(A, 4, "c")))
        return real_open(path, *a, **k)

    sim.D.open = fake_open
    try:
        got = tail.read()
    finally:
        del sim.D.open
    assert fired
    shown += ids(sim.deliver(A, "a", events=got))
    for _ in range(2):
        shown += ids(sim.deliver(A, "a", events=tail.read()))
    assert sorted(shown) == sorted(want)
    assert sim.deliver(A, "a") == []


GAP_READER = r'''
import importlib.util, json, os, sys, time
path, stop = sys.argv[1:3]
spec = importlib.util.spec_from_file_location("qed_rd", path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
parse = m.parse_lines
def slow_parse(data):                       # a reader that gets descheduled between its two file reads
    time.sleep(0.0004)
    return parse(data)
m.parse_lines = slow_parse
calls = gaps = 0
sample = None
while not os.path.exists(stop):
    seqs = sorted({e["seq"] for e in m.read_events()})
    calls += 1
    if seqs and seqs != list(range(seqs[0], seqs[-1] + 1)):
        gaps += 1
        sample = sample or seqs[:30]
print(json.dumps({"calls": calls, "gaps": gaps, "sample": sample}))
'''


def test_hook_reader_never_sees_a_hole_while_the_writer_keeps_rotating(tmp_path, procs):
    """A tight reader loop against the live writer, which rotates on nearly every poll (35 events, ~10 KB,
    against a 6 KB cap): what the reader returns must always be a gap-free run of seqs. A rotation that
    lands between the reader's two file reads shows up as a hole."""
    sim = Sim(tmp_path / "w")
    A = sim.sock(1821)
    sim.poll()
    sim.W.MAX_BYTES = 6000
    stop = tmp_path / "stop"
    out = tmp_path / "reader.out"
    reader = procs.popen([PY, script(tmp_path, "gap_reader.py", GAP_READER), str(DELIVER), str(stop)],
                         out=out, env=sim.env)
    inode, rotations, events = None, 0, 0
    for i in range(40):
        for j in range(35):
            sim.job(f"g{i}_{j}", "failed", A)
        events += sim.poll()
        ino = os.stat(sim.log).st_ino
        rotations += inode is not None and ino != inode
        inode = ino
        time.sleep(0.005)
    stop.touch()
    assert reader.wait(timeout=30) == 0
    res = json.loads(out.read_text())
    print(f"\n[rotation reader] events={events} rotations={rotations} reader_calls={res['calls']} gaps={res['gaps']}")
    assert events == 40 * 35 and rotations >= 30 and res["calls"] > 100, (events, rotations, res)
    assert res["gaps"] == 0, res


# -------------------------------------------------------------- 4. targeting and dead sessions
def test_dead_session_with_socket_gone_hands_its_events_to_a_live_peer_once(tmp_path):
    sim = Sim(tmp_path / "w")
    A, B = sim.sock(1901), sim.sock(1902)
    sim.poll()
    sim.deliver(A, "a")
    sim.deliver(B, "b")
    sim.job("d1", "failed", A)
    sim.job("d2", "passed", A)
    sim.poll()
    os.unlink(A)                              # A's process died with undelivered events
    got = sim.deliver(B, "b")
    assert ids(got) == ["d1", "d2"] and set(tags(got).values()) == {"launcher gone"}
    assert sim.deliver(B, "b") == []
    assert sim.deliver(sim.sock(1903), "c") == []   # a later new session: already shown, not again


def test_owner_that_dies_after_a_live_peer_looked_is_recoverable_only_by_a_new_session(tmp_path):
    """Design: a live peer skips (but advances past) another live session's events. If that owner then dies
    undelivered, the peer never shows them; only a brand-new session, which starts at the global cursor, does."""
    sim = Sim(tmp_path / "w")
    A, B = sim.sock(1911), sim.sock(1912)
    sim.poll()
    sim.deliver(A, "a")
    sim.deliver(B, "b")
    sim.job("e1", "failed", A)
    sim.poll()
    assert sim.deliver(B, "b") == []          # A is alive: not B's
    os.unlink(A)
    assert sim.deliver(B, "b") == []
    got = sim.deliver(sim.sock(1913), "c")
    assert ids(got) == ["e1"] and tags(got) == {"e1": "launcher gone"}
    assert sim.deliver(sim.sock(1913), "c") == [] and sim.deliver(B, "b") == []


def test_event_already_delivered_to_its_owner_is_not_replayed_to_a_new_session_after_the_owner_exits(tmp_path):
    """The owner got its events, then exited. A brand-new session starts at the global cursor, which only
    unowned events move, so it used to be shown them again as 'launcher gone'. An event the exiting owner
    never saw is still handed over, once."""
    sim = Sim(tmp_path / "w")
    A, D = sim.sock(2001), sim.sock(2002)
    sim.poll()
    sim.deliver(A, "a")
    sim.deliver(D, "d")
    sim.job("n1", "failed", A)
    sim.job("n2", "passed", A)
    sim.job("n3", "blocked", D)
    sim.poll()
    assert ids(sim.deliver(A, "a")) == ["n1", "n2"]   # n3 is D's, and D is alive
    os.unlink(A)
    os.unlink(D)                              # both exit; D never looked
    got = sim.deliver(sim.sock(2003), "c")
    assert ids(got) == ["n3"] and tags(got) == {"n3": "launcher gone"}
    assert sim.deliver(sim.sock(2003), "c") == [] and sim.deliver(sim.sock(2004), "e") == []


@pytest.mark.xfail(strict=True, reason="design: a live peer skips (and advances past) another live session's "
                   "events, and the global cursor is one high-water mark. If that owner exits before it looked "
                   "and an unowned event moved the global cursor past its event, no session is ever shown it.")
def test_owner_exiting_undelivered_after_the_global_cursor_passed_its_event_is_not_lost(tmp_path):
    sim = Sim(tmp_path / "w")
    A, B = sim.sock(2011), sim.sock(2012)
    sim.poll()
    sim.deliver(A, "a")
    sim.deliver(B, "b")
    sim.job("m1", "failed", A)
    sim.poll()
    assert sim.deliver(B, "b") == []          # A is alive: not B's
    sim.job("m2", "blocked", None)
    sim.poll()
    assert ids(sim.deliver(B, "b")) == ["m2"]  # unowned: the global cursor moves past m1
    os.unlink(A)                              # A exits without ever looking
    assert ids(sim.deliver(sim.sock(2013), "c")) == ["m1"]


@pytest.mark.xfail(strict=True, reason="design: liveness is 'the socket file exists'. A SIGKILLed session "
                   "leaves its socket behind, so its events look like a live peer's and nobody is shown them.")
def test_events_of_a_sigkilled_session_with_a_stale_socket_reach_a_new_session(tmp_path, procs):
    sim = Sim(tmp_path / "w")
    victim = procs.popen(["sleep", "60"])
    A, B = sim.sock(victim.pid), sim.sock(1921)
    sim.poll()
    sim.deliver(A, "a")
    sim.deliver(B, "b")
    sim.job("s1", "failed", A)
    sim.poll()
    victim.send_signal(signal.SIGKILL)
    victim.wait(timeout=10)                   # the pid is gone; its socket file is not
    assert os.path.exists(A)
    assert ids(sim.deliver(sim.sock(1922), "c")) == ["s1"]


def test_clear_keeps_the_process_cursor_nothing_lost_nothing_repeated(tmp_path):
    sim = Sim(tmp_path / "w")
    A = sim.sock(2301)
    sim.poll()
    assert sim.hook(A, "old") == []
    sim.job("c1", "failed", A)
    sim.poll()
    assert ids(sim.hook(A, "old")) == ["c1"]
    sim.job("c2", "passed", A)
    sim.poll()                                # arrives; the session is /cleared before its next hook
    assert ids(sim.hook(A, "new")) == ["c2"]  # new sid, same process: c1 not repeated, c2 not skipped
    assert sim.deliver(A, "old") == [] and sim.hook(A, "new") == []   # an old-sid watcher shares the process


def test_subagent_hook_calls_consume_nothing(tmp_path):
    sim = Sim(tmp_path / "w")
    A = sim.sock(2311)
    sim.poll()
    assert sim.hook(A, "a") == []
    sim.job("g1", "failed", A)
    sim.poll()
    assert sim.hook(A, "a", agent_id="sub-1") == []   # a subagent's tool call delivers and advances nothing
    assert ids(sim.hook(A, "a")) == ["g1"]            # the main session still gets it
    assert sim.hook(A, "a") == []


def test_pid_reuse_is_launcher_gone_not_yours(tmp_path):
    sim = Sim(tmp_path / "w")
    A = sim.sock(2321)
    sim.poll()
    sim.deliver(A, "a", bridge="new-bridge")
    sim.job("b1", "failed", A, session="old-bridge")   # same socket path, a previous session's job
    sim.job("b2", "failed", A, session="new-bridge")
    sim.poll()
    assert tags(sim.deliver(A, "a", bridge="new-bridge")) == {"b1": "launcher gone", "b2": "yours"}


def test_every_terminal_state_is_delivered_with_its_own_name(tmp_path):
    """One of each: done, passed, failed, blocked, needs_opus, cancelled (sidecars) and needs_info."""
    sim = Sim(tmp_path / "w")
    A = sim.sock(2331)
    sim.poll()
    assert sim.hook(A, "a") == []
    for kind in KINDS:
        sim.job("z" + kind, kind, A)
    sim.ask("zinfo", A)
    sim.poll()
    got = {e["id"]: e["state"] for e in map(parse_line, sim.hook(A, "a")) if e}
    assert got == {"z" + k: k for k in KINDS} | {"zinfo": "needs_info"}


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.exit(pytest.main([__file__, "-q", "-s", "-p", "no:cacheprovider"]))
