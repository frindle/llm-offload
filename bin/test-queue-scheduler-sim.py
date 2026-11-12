#!/usr/bin/env python3
"""H3: seeded deterministic simulation of the queue's commit/park/next-step state machine.

Drives the REAL pure functions bundle_commit_status / bundle_commit_step / settle_next_steps
(+ chain_run_progress / next_step_for_terminal) from ollama-queue.py against a modelled world:
jobs finishing / failing / escalating to needs_opus, chain drivers advancing / dying / vanishing,
cpu stages, human holds and focus overrides, force stops, daemon restarts. The composition per
tick mirrors _apply_bundle_commit (settle -> status_of -> step -> stale-park sweep -> launch).

Invariants (checked after every tick):
  S1 single lane owner        commit key never also parked; at most one running row
  S3 park hygiene             every park has why+kind; no parked bundle reads complete
  W1 work conservation        commit None => no bundle has a runnable pending row
  H1 no idle lane hold        a committed bundle with no running/pending row, no live driver, no
                              valid next-step declaration, no pending escalation heal is released
  L1P (default; --lenient skips) same, counting PARKED bundles with runnable work AND idle time across holder handoffs
  L2 no dead-declaration hold a 'continue' whose job is needs_opus never holds the lane
  C1 no cpu-wait hold         only a cpu stage outstanding (no row, no declaration) => released
  L1 bounded idle hold        a non-moving hold while another bundle is runnable lasts <= BUNDLE_IDLE_SOFT + slack
  L3 no ping-pong             scheduler transitions per bundle per hour <= 8 + 3*world changes
  L4 no starvation            after the faults stop and humans release holds, everything drains
  L5 restart equivalence      the same seed with daemon restarts injected yields the same trace
Usage: test-queue-scheduler-sim.py [--seeds N] [--hours H] [--seed S] [--replay S] [--queue PATH]
Exit 0 = all seeds clean; 1 = first failing seeds printed with a minimal replay command.
"""
import argparse, contextlib, importlib.util, io, json, os, random, shutil, sys, tempfile
from datetime import datetime, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--queue", default=str(Path(__file__).resolve().parent / "ollama-queue.py"))
ap.add_argument("--seeds", type=int, default=120)
ap.add_argument("--hours", type=float, default=2.0)
ap.add_argument("--seed", type=int, default=0, help="first seed")
ap.add_argument("--replay", type=int, default=None, help="run one seed verbosely")
ap.add_argument("--strict", action="store_true", default=True, help="assert L1P (parked-but-runnable bundles count as waiting) -- the DEFAULT since the 2026-10-09 hardening fixed both design gaps")
ap.add_argument("--lenient", dest="strict", action="store_false", help="skip L1P (pre-hardening behaviour)")
ap.add_argument("--max-report", type=int, default=5)
A = ap.parse_args()

T = Path(tempfile.mkdtemp(prefix="sched-sim-"))
os.environ["HOME"] = str(T)
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
ld = SourceFileLoader("queue_sim", A.queue)
q = importlib.util.module_from_spec(importlib.util.spec_from_loader("queue_sim", ld))
ld.exec_module(q)

DT = 15.0                       # one daemon tick of simulated time
T0 = 1_800_000_000.0            # epoch base (chain records carry ISO timestamps)
BUNDLES = ("A", "B", "C")
pk = lambda j: j.get("bundle")


def iso(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()


class World:
    def __init__(self, seed, restarts):
        self.r = random.Random(seed)            # world randomness (identical across restart runs)
        self.rr = random.Random(seed ^ 0x5EED)  # restart decisions only
        self.restarts = restarts
        self.now = T0
        self.rows, self.drv, self.cpu, self.esc = [], {}, {b: set() for b in BUNDLES}, {}
        self.state = {"jobs": self.rows}
        self.changes = {}                       # (bundle, hour) -> exogenous changes
        self.ev_log = {}                        # (bundle, hour) -> scheduler transitions
        self.nid = 0
        self.dir = Path(tempfile.mkdtemp(prefix="chain-", dir=str(T)))
        self.alive_pids = set()
        self.override = None
        self.trace, self.viol = [], []
        self.cov = {}
        self.idle = {}
        self.commit = None

    # ---- world mutators -------------------------------------------------------------
    def chg(self, b):
        k = (b, int((self.now - T0) // 3600))
        self.changes[k] = self.changes.get(k, 0) + 1

    def new_row(self, b, label=None, status="pending"):
        self.nid += 1
        row = {"id": f"j{self.nid}", "label": label or (f"auto-{b}-{self.nid}" if self.r.random() < 0.4 else f"manual-{b}-{self.nid}"), "bundle": b,
               "status": status, "user_hold": False, "created": self.now}
        self.rows.append(row)
        self.chg(b)
        return row

    def write_chain(self, b):
        d = self.drv.get(b)
        p = self.dir / f"{b}.json"
        if not d:
            return
        p.write_text(json.dumps({"phase": d["phase"], "pid": d["pid"], "step": "sim", "job": d["job"],
                                 "phase_since": iso(d["since"]), "updated_at": iso(d["since"])}))

    def start_driver(self, b, job):
        pid = 10_000 + self.nid * 7 + len(self.drv)
        self.alive_pids.add(pid)
        self.drv[b] = {"pid": pid, "phase": "waiting", "job": job, "since": self.now,
                       "decide_at": None, "rounds": self.r.randint(0, 3)}
        self.write_chain(b)

    def declare(self, row):
        """what the daemon does at terminal reap (_declare_terminal_next_step)."""
        b = row["bundle"]
        ch = q.chain_run_progress(b, runs_dir=str(self.dir), alive=lambda p: p in self.alive_pids, now=self.now)
        self.state.setdefault("_bundle_next", {})[b] = q.next_step_for_terminal(row, ch, self.now)

    def world_events(self):
        r, now = self.r, self.now
        for row in list(self.rows):
            b = row["bundle"]
            if row["status"] == "running" and now >= row["end_at"]:
                row["status"] = r.choices(["done", "failed", "needs_opus"], [55, 25, 20])[0]
                if row["status"] == "needs_opus":
                    self.esc[row["id"]] = r.choice(["pending", None, None])
                self.declare(row)
                if row["status"] == "needs_opus" and r.random() < 0.3 and b in self.drv:
                    # smoke 2026-10-09: the driver gave up on the escalated row and its record is gone
                    self.drv.pop(b)
                    try:
                        (self.dir / f"{b}.json").unlink()
                    except OSError:
                        pass
                self.chg(b)
                d = self.drv.get(b)
                if d and d["job"] == row["id"] and d["phase"] == "waiting" and r.random() < 0.4:
                    pass                    # driver missed the event: stays 'waiting' on a terminal row
                elif d and d["job"] == row["id"] and d["phase"] == "waiting":
                    d["phase"], d["since"], d["decide_at"] = "advancing", now, now + r.choice([10, 40, 90, 200])
                    self.write_chain(b)
            elif row["status"] == "running" and r.random() < 0.004:      # force stop
                row["status"] = "failed"
                self.declare(row)
                self.chg(b)
            elif row["status"] == "failed" and r.random() < 0.01:        # ladder escalates it
                row["status"] = "needs_opus"
                self.esc[row["id"]] = r.choice(["pending", None])
                self.chg(b)
            elif row["status"] == "needs_opus":
                if self.esc.get(row["id"]) == "pending" and r.random() < 0.02:
                    self.esc[row["id"]] = r.choice(["cleared", None])
                    self.chg(b)
                elif r.random() < 0.004:
                    row["status"] = "done"
                    self.chg(b)
            elif row["status"] == "pending" and r.random() < 0.004:      # human hold toggle
                row["user_hold"] = not row["user_hold"]
                self.chg(b)
            elif row["status"] == "pending" and row["user_hold"] and r.random() < 0.02:
                row["user_hold"] = False
        for b, d in list(self.drv.items()):
            alive = d["pid"] in self.alive_pids
            if alive and d["phase"] == "advancing" and d["decide_at"] is not None and now >= d["decide_at"]:
                if d["rounds"] > 0 and r.random() < 0.8:
                    d["rounds"] -= 1
                    row = self.new_row(b, label=f"auto-{b}-c{self.nid + 1}")
                    d["phase"], d["job"], d["since"], d["decide_at"] = "waiting", row["id"], now, None
                else:
                    d["phase"], d["since"], d["decide_at"] = "ended", now, None
                self.write_chain(b)
                self.chg(b)
            elif alive and r.random() < 0.003:                           # driver dies (orphan record)
                self.alive_pids.discard(d["pid"])
                self.chg(b)
            elif r.random() < 0.003:                                     # record vanishes
                try:
                    (self.dir / f"{b}.json").unlink()
                except OSError:
                    pass
                self.drv.pop(b, None)
                self.chg(b)
        for b in BUNDLES:
            if r.random() < 0.012:
                row = self.new_row(b)
                if b not in self.drv and r.random() < 0.5:
                    self.start_driver(b, row["id"])
            if r.random() < 0.01 and not self.cpu[b]:
                self.cpu[b].add(f"cpu{self.nid}-{b}")
                self.chg(b)
            elif self.cpu[b] and r.random() < 0.03:
                self.cpu[b].clear()
                self.chg(b)
        if r.random() < 0.002:
            self.override = r.choice(BUNDLES)
            self.chg(self.override)

    # ---- one daemon tick (mirrors _apply_bundle_commit) -----------------------------------
    def tick(self, drain=False):
        st, now = self.state, self.now
        jobs = self.rows
        parked = st.setdefault("_bundle_parked", {})
        prev = st.get("_bundle_commit") or None
        alive = lambda p: p in self.alive_pids
        q.settle_next_steps(st, pk, now, chain_dir=str(self.dir), alive=alive)
        nxt = st.get("_bundle_next") or {}
        cache = {}

        def status_of(k):
            if k not in cache:
                ch = q.chain_run_progress(k, runs_dir=str(self.dir), alive=alive, now=now)
                cache[k] = q.bundle_commit_status(
                    k, jobs, pk, {}, ch, esc_state=lambda j: self.esc.get(j["id"]),
                    cpu_wait=sorted(self.cpu.get(k) or ()), next_step=nxt.get(k), now=now)
            return cache[k]

        runnable = lambda b: [j for j in jobs if j["bundle"] == b and j["status"] == "pending" and not j["user_hold"]]
        cands = []
        for j in jobs:
            if j["status"] == "pending" and not j["user_hold"] and j["bundle"] not in cands:
                cands.append(j["bundle"])
        running = [j for j in jobs if j["status"] == "running"]
        running_key = running[0]["bundle"] if running else None
        ov, self.override = self.override, None
        commit, events = q.bundle_commit_step(dict(prev) if prev else None, parked, running_key, cands,
                                              status_of, now, override_key=ov, skip_grace=lambda _k: True,
                                              **({"hist": st.setdefault("_bundle_flaps", {})} if "hist" in __import__("inspect").signature(q.bundle_commit_step).parameters else {}))
        ck = (commit or {}).get("key")
        for pkey in [x for x in list(parked) if x != ck]:      # stale-park sweep
            if status_of(pkey)[0] == "complete":
                parked.pop(pkey, None)
                events.append(("unpark", pkey, "swept"))
        st["_bundle_commit"] = commit
        self.commit = ck
        for ev, k, _w in events:
            self.cov[ev] = self.cov.get(ev, 0) + 1
            h = (k, int((now - T0) // 3600))
            self.ev_log[h] = self.ev_log.get(h, 0) + 1
        if ck is not None and not running:                      # launch (focus: committed bundle only)
            rr = runnable(ck)
            if rr:
                rr[0]["status"], rr[0]["end_at"] = "running", now + self.r.choice([30, 90, 240, 600])
        self.check(ck, events, runnable, status_of, parked, jobs)
        self.trace.append((ck, tuple(sorted(parked))))
        if self.restarts and self.rr.random() < 0.01:           # daemon restart: only persisted state survives
            for key in ("_bundle_commit", "_bundle_parked", "_bundle_next", "_bundle_esc_seen", "_bundle_flaps"):
                if key in st:
                    st[key] = json.loads(json.dumps(st[key]))

    def bad(self, code, msg):
        self.viol.append(f"{code} t={int(self.now - T0)}s: {msg}")

    def check(self, ck, events, runnable, status_of, parked, jobs):
        now = self.now
        running = [j for j in jobs if j["status"] == "running"]
        if len(running) > 1:
            self.bad("S1", f"{len(running)} rows running at once")
        if ck is not None and ck in parked:
            self.bad("S1", f"{ck} is committed and parked")
        for k, p in parked.items():
            if not p.get("why") or not p.get("kind"):
                self.bad("S3", f"park of {k} lacks why/kind: {p}")
            if status_of(k)[0] == "complete":
                self.bad("S3", f"parked {k} reads complete and was not dropped")
        if ck is None:
            for b in BUNDLES:
                if runnable(b):
                    self.bad("W1", f"no commit but {b} has a runnable pending row")
        if ck is not None:
            b = ck
            rows = [j for j in jobs if j["bundle"] == b]
            live = [j for j in rows if j["status"] in ("running", "pending") and not j["user_hold"]]
            d = self.drv.get(b)
            drv_live = bool(d and d["pid"] in self.alive_pids and d["phase"] in ("advancing", "waiting")
                            and (d["phase"] == "waiting" or now - d["since"] < q.CHAIN_ADVANCE_CEILING))
            decl = (self.state.get("_bundle_next") or {}).get(b)
            decl_ok = bool(decl and decl.get("kind") == "continue" and now < float(decl.get("expires_at") or 0))
            decl_dead = decl_ok and any(j["id"] == decl.get("job") and j["status"] == "needs_opus" for j in rows)
            esc_pending = any(j["status"] == "needs_opus" and self.esc.get(j["id"]) == "pending" for j in rows)
            if decl_dead and not live and not drv_live:
                self.bad("L2", f"{b} holds the lane on a continue declaration for a needs_opus job")
            decl_legit = decl_ok and not decl_dead
            if self.cpu[b] and not live and not decl_legit and not decl_dead:
                self.bad("C1", f"{b} holds the lane while only cpu stage(s) {sorted(self.cpu[b])} are outstanding")
            elif not (live or drv_live or decl_legit or esc_pending) and not self.cpu[b]:
                self.bad("H1", f"{b} holds the lane with no row, driver, declaration or heal pending "
                               f"(drv={d and (d['phase'], d['pid'] in self.alive_pids)}, decl={decl and decl.get('kind')})")
        _mv = status_of(ck)[2] if ck is not None else False
        self.last_ck = getattr(self, "last_ck", None)
        for code, pool in (("L1", [b for b in BUNDLES if b != ck and b not in parked]),
                           ("L1P", [b for b in BUNDLES if b != ck])):
            idle = (not running and any(runnable(b) for b in pool)
                    and (ck is None or not runnable(ck)) and not _mv)
            if code == "L1" and ck != self.last_ck:
                self.idle[code] = None      # a new holder gets its own idle clock (by contract)
            self.idle[code] = (self.idle.get(code) or now) if idle else None
            if self.idle[code] is not None and now - self.idle[code] > 600.0 + 4 * DT:   # documented 10 min soft ceiling; literal so a mutated constant cannot hide
                if code == "L1" or A.strict:
                    self.bad(code, f"lane held idle (not moving) {int(now - self.idle[code])}s with "
                                   f"{'a non-parked' if code == 'L1' else 'any (incl. parked)'} bundle runnable (commit={ck})")
        self.last_ck = ck

    def finish_checks(self):
        for (b, h), n in self.ev_log.items():
            bound = 8 + 3 * self.changes.get((b, h), 0)
            if n > bound:
                self.bad("L3", f"bundle {b} had {n} scheduler transitions in hour {h} (world changes "
                               f"{self.changes.get((b, h), 0)}, bound {bound}) -- ping-pong")


def run(seed, hours, restarts=False, verbose=False):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        w = _run(seed, hours, restarts)
    if verbose:
        for v in w.viol[:40]:
            print("   ", v)
    return w


def _run(seed, hours, restarts=False):
    w = World(seed, restarts)
    steps = int(hours * 3600 / DT)
    for _ in range(steps):
        w.now += DT
        w.world_events()
        w.tick()
        if w.viol and len(w.viol) > 40:
            break
    # drain: faults stop, humans release holds, drivers end -> everything must finish (L4)
    for j in w.rows:
        j["user_hold"] = False
    for b in list(w.drv):
        w.alive_pids.discard(w.drv[b]["pid"])
    for b in BUNDLES:
        w.cpu[b].clear()
    for _ in range(int(8 * 3600 / DT)):
        w.now += DT
        for row in list(w.rows):
            if row["status"] == "running" and w.now >= row["end_at"]:
                row["status"] = "done"
                w.declare(row)
        for row in w.rows:
            if row["status"] == "needs_opus":
                w.esc[row["id"]] = "cleared"
        w.tick(drain=True)
    left = [j["id"] for j in w.rows if j["status"] in ("pending", "running")]
    if left:
        w.bad("L4", f"starvation: {len(left)} runnable rows never ran after the drain ({left[:4]}); commit={w.commit}")
    w.finish_checks()
    return w


def main():
    if A.replay is not None:
        w = run(A.replay, A.hours, verbose=True)
        print("viol:", len(w.viol))
        return 1 if w.viol else 0
    failures, cov = [], {}
    for s in range(A.seed, A.seed + A.seeds):
        w = run(s, A.hours)
        w2 = run(s, A.hours, restarts=True)
        for k_, n_ in w.cov.items():
            cov[k_] = cov.get(k_, 0) + n_
        v = list(w.viol)
        if w.trace != w2.trace:
            i = next(i for i, (a, b) in enumerate(zip(w.trace, w2.trace)) if a != b)
            v.append(f"L5 restart equivalence broken at tick {i}: plain={w.trace[i]} restarted={w2.trace[i]}")
        if v:
            failures.append((s, v))
    classes = {}
    for _s, v in failures:
        for x in v:
            classes.setdefault(x.split()[0], []).append(_s)
    for s, v in failures[:A.max_report]:
        print(f"FAIL seed={s} ({len(v)} violation(s)); replay: python3 {sys.argv[0]} --replay {s} --hours {A.hours}")
        for x in v[:3]:
            print("   ", x)
    print("coverage (scheduler events seen): " + ", ".join(f"{k}={v}" for k, v in sorted(cov.items())))
    print(f"{A.seeds - len(failures)}/{A.seeds} seeds clean; failing classes: "
          + (", ".join(f"{c} x{len(set(ss))} (e.g. seed {ss[0]})" for c, ss in sorted(classes.items())) or "none"))
    shutil.rmtree(T, ignore_errors=True)
    if not failures:
        print("ALL PASS")
    return 1 if failures else 0


sys.exit(main())
