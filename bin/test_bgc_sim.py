#!/usr/bin/env python3
"""Simulated stress run of the queue's BloomGauge control: the real bloomgauge_tick / bloomgauge_policy /
bloomgauge_control code and the real stale-lease guard (bloomgauge_lease_guard) against SimBG, a fake BloomGauge
that copies the app's own rules, on a simulated clock. No network, no real BloomGauge, temp files only.

SimBG copies, from the app bundle (2026-11-02):
  - agent_access.AgentAccess.admit/refund: 2 model changes and 6 Manager changes per rolling hour for all agents
    together, a 4xx refusal given back, a failure (5xx) still counted, the count reset when the app restarts or
    agent changes are turned off; over the limit a 429 with the app's own text (RATE_429);
  - the activity-log rows each change writes: an agent's change agent.request (+ control.request for a Manager
    change, same second), the owner's control.request, then mode.change (why user-on/user-off/pick/recovery/
    blocked) on the optimizer's next tick; a row lands when the request is answered (0.5-12.4 s, as seen), its
    'at' whole seconds; the newest `limit` rows of the manager family, newest first;
  - agent_mcp: a pick of a model the Mac cannot serve raises ValueError before anything is sent.

Days of random queue bursts (several sessions), owner changes (Manager on/off, manual picks), other agents,
BloomGauge's own Manager changes (recovery, blocked) and model switches, refusals, failed writes, an unreadable
log, app restarts, agent changes turned off, daemon restarts mid-job and daemon deaths (the guard runs every
300 s, as its LaunchAgent does), then a quiet tail. Checks, on what the fake recorded (never on what the queue
believes):
  rate      no queue or guard write refused with 429; own attempts within cfg mgr_per_h / picks_per_h in every
            rolling hour
  owner     no queue or guard write within yield_s after an owner's (or another agent's) change once its log
            row landed; a Manager the owner turned off is never turned on again by the queue unless a work cycle
            (a lease) started after that off (handback 'always', by design)
  log       no write while the activity log is unreadable, none while BloomGauge is mid-change
  handback  a Manager the queue turned off is on again within HANDBACK_S of healthy queue idle (daemon alive)
            or GUARD_S of healthy time with the daemon dead (the guard), and on at the end of the quiet tail
  alerts    one alert per failure streak of ALERT_STREAK+ writes of a kind; at most one per unreadable-log spell
Races the queue cannot see (a write between an owner's change and its log row) are counted, not failed.

Run: python3 test_bgc_sim.py [--days N] [--seeds 1,2,3] [-v]   (exit 1 on any violation)
"""
import argparse
import bisect
import heapq
import importlib
import importlib.util
import json
import os
import random
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import bloomgauge_control  # noqa: E402
import bloomgauge_lease_guard  # noqa: E402
import bloomgauge_policy  # noqa: E402
import bloomgauge_tick  # noqa: E402
import escalation_index_janitor  # noqa: E402

T0 = 1793520000.0
HOUR = 3600.0
MODELS = ("m1", "m2", "m3")
CHANGES = ("change-model", "restart")
NOT_ANSWERING = "BloomGauge is not answering on this Mac. Open the BloomGauge app and try again."
TURN_ON = "Turn on More > Settings > Let your agent make changes in BloomGauge first."
RATE_429 = ("Agents can switch models or restart Darkbloom at most 2 times an hour, and change the Manager at "
            "most 6 times. Try again in about {m} min.")
TICK_S = 30.0
GUARD_EVERY_S = 300.0
JANITOR_S = 300.0        # com.example.dispatch-escalation-watcher StartInterval: it runs escalation_index_janitor
JANITOR_PHASE_S = 137.0  # its runs fall at JANITOR_PHASE_S mod JANITOR_S
TAIL_S = 8 * HOUR


class Unavailable(Exception):
    pass


class Clock:
    def __init__(self):
        self.t = T0


class Truth:
    """What really happened, as the fake saw it."""

    def __init__(self):
        self.mgr = []      # (t, on, who, why): every Manager change
        self.manual = []   # (t, seen_at, what, who): changes the queue must treat as manual
        self.writes = []   # (t, caller, action, reached, log_bad, switching)
        self.r429 = []     # (t, caller, what)
        self.alerts = []   # (t, key, msg): what the tick raised (before the queue's dedupe)
        self.leases = []   # (t, since): a lease the queue holds, as saved after a tick (since changes on acquire)
        self.spans = []    # [since, end]: each lease from its since until it was seen gone (end None: still held)
        self.bad_log = []  # [start, end]: spells of an unreadable log while the app answers
        self.modes = []    # [start, end, mode]: spells with control set to off or shadow


class SimBG:
    """A fake BloomGauge on clock c. The queue's client (get / set_manager / pick_model / Unavailable)."""
    Unavailable = Unavailable

    def __init__(self, clock, rng, truth):
        self.c, self.rng, self.truth = clock, rng, truth
        self.manager_on = True
        self.current, self.serving = "m3", ["m3"]
        self.switch_until, self.pending = 0.0, None
        self.down = self.log_bad = False
        self.agent_on = True
        self.can_enable, self.blocker = True, None
        self.fail_p = 0.0
        self.recent = []                 # agent_access.AgentAccess.recent: (t, what) let through this hour
        self.lands, self.rows = [], []   # activity-log rows, sorted by when they land
        self.caller = "queue"

    # -- the app's rules
    def _admit(self, what):
        now = self.c.t
        self.recent = [(t, w) for t, w in self.recent if 0 <= now - t < HOUR]
        change = what in CHANGES
        same = [w for _, w in self.recent if (w in CHANGES) == change]
        if len(same) >= (2 if change else 6):
            return None
        e = (now, what)
        self.recent.append(e)
        return e

    def _refund(self, e):
        if e in self.recent:
            self.recent.remove(e)

    def _resets_in(self, what):
        times = [t for t, w in self.recent if (w in CHANGES) == (what in CHANGES)]
        return max(1, -(-int(HOUR - (self.c.t - min(times))) // 60)) if times else 0

    def row(self, event, lag, **fields):
        land = self.c.t + max(0.0, lag)
        i = bisect.bisect_right(self.lands, land)
        self.lands.insert(i, land)
        self.rows.insert(i, {"at": float(int(land)), "event": event, "fields": fields})
        return land

    def flip(self, on, why, who, lag):
        """The Manager to on/off; its mode.change lands on the optimizer's next tick. -> that land time."""
        if self.manager_on == on:
            return None
        self.manager_on = on
        self.truth.mgr.append((self.c.t, on, who, why))
        return self.row("mode.change", lag + self.rng.uniform(0.5, 10.0),
                        **{"from": "off" if on else "on", "to": "on" if on else "off", "why": why})

    def switching(self):
        return self.c.t < self.switch_until

    def _settle(self):
        if self.pending is not None and self.c.t >= self.switch_until:
            self.current, self.serving, self.pending = self.pending, [self.pending], None

    def start_switch(self, model, lag):
        self.switch_until = self.c.t + lag + self.rng.uniform(20.0, 120.0)
        self.pending = model

    # -- the agent client (agent_mcp.py's surface)
    def get(self, path, params=None):
        if self.down:
            raise Unavailable(NOT_ANSWERING)
        self._settle()
        if path == "/api/model-control":
            return {"currentModel": self.current, "servingModels": list(self.serving), "switching": self.switching(),
                    "providerControl": {"status": "running"}, "models": [{"id": m} for m in MODELS]}
        if path == "/api/optimizer/control":
            auto = {"mode": "auto" if self.manager_on else "manual", "canEnable": self.can_enable}
            if self.blocker:
                auto["blocker"] = {"code": self.blocker}
            return {"automatic": auto, "operation": {"status": "active"}, "currentModel": self.current}
        if path == "/api/agent-access":
            return {"enabled": self.agent_on, "keyPath": "/sim/agent-key" if self.agent_on else None}
        if path == "/api/activity/log":
            if self.log_bad:
                raise Unavailable("BloomGauge answered 500 for /api/activity/log.")
            n = bisect.bisect_right(self.lands, self.c.t)
            lim = int((params or {}).get("limit") or 100)
            return {"events": self.rows[max(0, n - lim):n][::-1]}
        raise AssertionError("unexpected path " + path)

    def _write(self, action):
        self.truth.writes.append((self.c.t, self.caller, action, not self.down and self.agent_on, self.log_bad,
                                  self.switching()))

    def set_manager(self, args):
        on = args.get("on")
        if type(on) is not bool:
            raise ValueError("on must be true or false.")
        self._write("mgr_on" if on else "mgr_off")
        if self.down:
            raise Unavailable(NOT_ANSWERING)
        if not self.agent_on:
            raise Unavailable(TURN_ON)
        self._settle()
        lag = self.rng.uniform(0.5, 12.4)
        yn = "yes" if on else "no"
        e = self._admit("manager")
        if e is None:
            self.row("agent.request", lag, what="manager", model=None, result="refused")
            self.truth.r429.append((self.c.t, self.caller, "manager"))
            raise Unavailable(RATE_429.format(m=self._resets_in("manager")))
        if on and not self.can_enable:
            self._refund(e)
            self.row("agent.request", lag, what="manager", model=None, result="refused")
            self.row("control.request", lag, what="manager", model=None, on=yn, why="user", result="refused",
                     **{"from": "mac"})
            raise Unavailable("The Manager can't start: " + str(self.blocker or "blocked"))
        if self.rng.random() < self.fail_p:
            self.row("agent.request", lag, what="manager", model=None, result="failed")
            self.row("control.request", lag, what="manager", model=None, on=yn, why="user", result="failed",
                     **{"from": "mac"})
            raise Unavailable("BloomGauge answered 500 for /api/optimizer/control.")
        self.row("agent.request", lag, what="manager", model=None, result="ok")
        self.row("control.request", lag, what="manager", model=None, on=yn, why="user", result="ok", **{"from": "mac"})
        self.flip(on, "user-on" if on else "user-off", self.caller, lag)
        return {"ok": True}

    def pick_model(self, args):
        model = args.get("model")
        if self.down:
            raise Unavailable(NOT_ANSWERING)
        self._settle()
        if model not in MODELS:
            raise ValueError(f"{model} is not a model this Mac can serve. See get_manager.")
        self._write("pick")
        if not self.agent_on:
            raise Unavailable(TURN_ON)
        lag = self.rng.uniform(0.5, 12.4)
        e = self._admit("change-model")
        if e is None:
            self.row("agent.request", lag, what="change-model", model=model, result="refused")
            self.truth.r429.append((self.c.t, self.caller, "change-model"))
            raise Unavailable(RATE_429.format(m=self._resets_in("change-model")))
        if self.switching():
            self._refund(e)
            self.row("agent.request", lag, what="change-model", model=model, result="refused")
            raise Unavailable("BloomGauge is changing models; try again in a minute.")
        if self.rng.random() < self.fail_p:
            self.row("agent.request", lag, what="change-model", model=model, result="failed")
            raise Unavailable("BloomGauge answered 500 for /api/model-control.")
        self.row("agent.request", lag, what="change-model", model=model, result="ok")
        if self.manager_on:
            self.flip(False, "pick", self.caller, lag)
        self.start_switch(model, lag)
        return {"ok": True}

    # -- everyone else
    def owner_toggle(self):
        on = not self.manager_on
        if self.down or (on and not self.can_enable):
            return False
        lag = self.rng.uniform(0.0, 3.0)
        seen = self.row("control.request", lag, what="manager", model=None, on="yes" if on else "no", why="user",
                        result="ok", **{"from": "mac"})
        self.flip(on, "user-on" if on else "user-off", "owner", lag)
        self.truth.manual.append((self.c.t, seen, "toggle-" + ("on" if on else "off"), "owner"))
        return True

    def owner_pick(self):
        self._settle()
        if self.down or self.switching():
            return False
        model = self.rng.choice([m for m in MODELS if m != self.current])
        lag = self.rng.uniform(0.0, 3.0)
        seen = self.row("control.request", lag, what="manual", model=model, why="user", result="ok", **{"from": "mac"})
        if self.manager_on:
            seen = min(seen, self.flip(False, "pick", "owner", lag))
        self.start_switch(model, lag)
        self.truth.manual.append((self.c.t, seen, "pick", "owner"))
        return True

    def agent_toggle(self):
        if self.down or not self.agent_on:
            return False
        on = not self.manager_on
        lag = self.rng.uniform(0.5, 12.4)
        if self._admit("manager") is None:
            self.row("agent.request", lag, what="manager", model=None, result="refused")
            return False
        if on and not self.can_enable:
            return False
        seen = self.row("agent.request", lag, what="manager", model=None, result="ok")
        self.row("control.request", lag, what="manager", model=None, on="yes" if on else "no", why="user",
                 result="ok", **{"from": "mac"})
        self.flip(on, "user-on" if on else "user-off", "agent", lag)
        self.truth.manual.append((self.c.t, seen, "agent-toggle", "agent"))
        return True

    def agent_pick(self):
        self._settle()
        if self.down or not self.agent_on or self.switching():
            return False
        model = self.rng.choice(MODELS)
        lag = self.rng.uniform(0.5, 12.4)
        if self._admit("change-model") is None:
            self.row("agent.request", lag, what="change-model", model=model, result="refused")
            return False
        self.row("agent.request", lag, what="change-model", model=model, result="ok")
        if self.manager_on:  # the pick flips the Manager off: a mode.change no queue attempt explains
            land = self.flip(False, "pick", "agent", lag)
            self.truth.manual.append((self.c.t, land, "agent-pick", "agent"))
        self.start_switch(model, lag)
        return True

    def app_recovery(self):
        if self.down or self.manager_on or not self.can_enable:
            return False
        self.flip(True, "recovery", "app", 0.0)
        return True

    def manager_switch(self):
        """The Manager, while on, moves to another model by itself."""
        self._settle()
        if self.down or not self.manager_on or self.switching():
            return False
        self.start_switch(self.rng.choice([m for m in MODELS if m != self.current]), 0.0)
        self.row("manager.decide", 0.0, decision="move")
        return True


class Daemon:
    """The queue daemon's BloomGauge control: fresh modules on every (re)start, as a new process has."""

    def __init__(self):
        self.alive = True
        self.restarts = 0
        self.start()

    def start(self):
        self.ctl = importlib.reload(bloomgauge_control)
        self.pol = importlib.reload(bloomgauge_policy)
        self.bgt = importlib.reload(bloomgauge_tick)
        self.restarts += 1


def _guard_modules():
    """A separate copy of bloomgauge_control/policy/tick for the guard (its own process under launchd)."""
    mods = []
    for name in ("bloomgauge_control", "bloomgauge_policy", "bloomgauge_tick"):
        spec = importlib.util.spec_from_file_location("guard_" + name, HERE / (name + ".py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        mods.append(m)
    return mods


# Mean hours between events (exponential), per profile. 'hostile' packs a month of trouble into days.
PROFILES = {
    "normal": {"burst": 3, "owner_toggle": 5, "owner_pick": 14, "agent_toggle": 30, "agent_pick": 30,
               "recovery": 10, "mgr_switch": 2, "restart": 14, "corrupt": 96, "blocked": 30, "app_down": 30,
               "agent_off": 80, "log_bad": 30, "fail": 16, "death": 40, "mode": 72},
    "hostile": {"burst": 1.5, "owner_toggle": 1.5, "owner_pick": 4, "agent_toggle": 5, "agent_pick": 5,
                "recovery": 4, "mgr_switch": 1, "restart": 4, "corrupt": 24, "blocked": 8, "app_down": 8,
                "agent_off": 24, "log_bad": 10, "fail": 6, "death": 6, "mode": 24},
    # the stale-lease guard's own path: the daemon dies often and stays dead for hours while it holds the
    # Manager off, and the owner picks or toggles meanwhile
    "deadhold": {"burst": 1, "owner_toggle": 3, "owner_pick": 1.5, "agent_toggle": 10, "agent_pick": 10,
                 "recovery": 8, "mgr_switch": 2, "restart": 24, "corrupt": 48, "blocked": 48, "app_down": 48,
                 "agent_off": 96, "log_bad": 48, "fail": 24, "death": 1.5, "mode": 96},
}


class Sim:
    def __init__(self, seed, days, home, verbose=False, profile="normal"):
        self.rng = random.Random(seed)
        self.seed, self.days, self.home, self.verbose = seed, days, home, verbose
        self.profile, self.rates = profile, PROFILES[profile]
        self.clock = Clock()
        self.truth = Truth()
        self.bg = SimBG(self.clock, random.Random(seed * 7919 + 1), self.truth)
        self.daemon = Daemon()
        self.gctl, self.gpol, self.gtk = _guard_modules()
        env = {"QUEUE_BLOOMGAUGE_CONTROL": "on"}
        self.cfg = self.daemon.ctl.config(env=env, home=home)
        self.jobs = []           # [remaining_s, target]
        self.heap = []
        self.seq = 0
        self.end_busy = T0 + days * 86400
        self.end = self.end_busy + TAIL_S
        self.last_tick = None
        self.violations = []
        self.races = []
        self.stats = {"bursts": 0, "jobs": 0, "busy_s": 0.0, "busy_mgr_off_s": 0.0, "restarts": 0, "deaths": 0,
                      "owner": 0, "agent": 0, "ticks": 0, "guard_runs": 0, "guard_acts": 0, "guard_yields": 0,
                      "kept_yields": 0}
        # hand-back watch: how long the queue's own off has held while it should be handed back
        self.hb_run = 0.0
        self.guard_run = 0.0
        self.max_hb = 0.0

    # -- scheduling
    def at(self, t, fn, *args):
        self.seq += 1
        heapq.heappush(self.heap, (t, self.seq, fn, args))

    def every(self, mean_s, fn, *args):
        """fn at exponential intervals (mean mean_s) until end_busy."""
        def fire():
            fn(*args)
            nxt = self.clock.t + self.rng.expovariate(1.0 / mean_s)
            if nxt < self.end_busy:
                self.at(nxt, fire)
        self.at(T0 + self.rng.expovariate(1.0 / mean_s), fire)

    def spell(self, mean_s, lo, hi, on, off):
        """on() at exponential intervals, off() lo..hi s later (spells never overlap)."""
        def fire():
            on()
            d = self.rng.uniform(lo, hi)
            self.at(self.clock.t + d, finish)
        def finish():
            off()
            nxt = self.clock.t + self.rng.expovariate(1.0 / mean_s)
            if nxt < self.end_busy:
                self.at(nxt, fire)
        self.at(T0 + self.rng.expovariate(1.0 / mean_s), fire)

    # -- the world
    def burst(self):
        n = self.rng.randint(1, 8)
        target = self.rng.choices(["m1", "m2", None, "m9"], weights=[45, 45, 6, 4])[0]
        for _ in range(n):
            self.jobs.append([self.rng.uniform(120, 1500), target])
        self.stats["bursts"] += 1
        self.stats["jobs"] += n

    def owner_toggle(self):
        if self.bg.owner_toggle():
            self.stats["owner"] += 1

    def owner_pick(self):
        if self.bg.owner_pick():
            self.stats["owner"] += 1

    def agent_toggle(self):
        if self.bg.agent_toggle():
            self.stats["agent"] += 1

    def agent_pick(self):
        if self.bg.agent_pick():
            self.stats["agent"] += 1

    def block_on(self):
        bg = self.bg
        if not bg.down:
            bg.flip(False, "blocked", "app", 0.0)
        bg.can_enable, bg.blocker = False, "memory"

    def block_off(self):
        bg = self.bg
        bg.can_enable, bg.blocker = True, None
        if not bg.down and self.rng.random() < 0.5:
            bg.flip(True, "recovery", "app", 0.0)

    def app_down(self):
        self.bg.down = True

    def app_up(self):
        bg = self.bg
        bg.down = False
        bg.recent = []          # a new process: the hourly counts start again

    def agent_off(self):
        self.bg.agent_on = False
        self.bg.recent = []     # agent_access.set_enabled(False) clears them

    def agent_on(self):
        self.bg.agent_on = True

    def log_off(self):
        self.bg.log_bad = True
        self.truth.bad_log.append([self.clock.t, None])

    def log_on(self):
        self.bg.log_bad = False
        self.truth.bad_log[-1][1] = self.clock.t

    def fail_on(self):
        self.bg.fail_p = self.rng.choice([0.3, 0.6, 1.0])

    def fail_off(self):
        self.bg.fail_p = 0.0

    def daemon_restart(self):
        if self.daemon.alive:
            self.daemon.start()
            self.stats["restarts"] += 1

    def daemon_die(self):
        if self.daemon.alive:
            self.daemon.alive = False
            self.stats["deaths"] += 1

    def daemon_back(self):
        if not self.daemon.alive:
            self.daemon.alive = True
            self.daemon.start()
            self.last_tick = None

    def mode_away(self):
        """The owner sets control to off or shadow (the mode file) for a while."""
        self.cfg["mode"] = self.rng.choice(["off", "shadow"])
        self.truth.modes.append([self.clock.t, None, self.cfg["mode"]])

    def mode_on(self):
        self.cfg["mode"] = "on"
        self.truth.modes[-1][1] = self.clock.t

    def corrupt_lease(self):
        p = Path(self.cfg["lease_path"])
        if p.exists():
            p.write_text("{not json")
            os.utime(p, (self.clock.t, self.clock.t))   # lease_load reads 'since' from the mtime

    def filler(self):
        self.bg.row(self.rng.choice(["manager.decide", "op.step", "recommend.hour"]), 0.0, x="y")

    # -- the daemon's tick and the guard
    def tick(self):
        now = self.clock.t
        d = self.daemon
        if d.alive:
            dt = 0.0 if self.last_tick is None else now - self.last_tick
            self.last_tick = now
            while self.jobs and dt > 0:
                use = min(dt, self.jobs[0][0])
                self.jobs[0][0] -= use
                dt -= use
                if self.jobs[0][0] <= 0:
                    self.jobs.pop(0)
            busy = bool(self.jobs)
            work = {"busy": busy, "why": "sim", "target": self.jobs[0][1] if busy else None}
            self.bg.caller = "queue"
            if d.bgt.wanted(dict(self.cfg), d.ctl):   # as ollama-queue.py bloomgauge_hook
                d.bgt.tick(work, now, dict(self.cfg), self.bg, d.pol, d.ctl)
            else:
                d.bgt._STATE.update(idle_since=None, next_poll=0.0)
            alert = d.bgt._STATE.pop("alert", None)
            if isinstance(alert, dict):
                self.truth.alerts.append((now, alert.get("key"), alert.get("msg")))
            lease = d.ctl.lease_load(self.cfg["lease_path"])
            if isinstance(lease, dict) and not lease.get("corrupt"):
                if not self.truth.leases or self.truth.leases[-1][1] != lease.get("since"):
                    self.truth.leases.append((now, lease.get("since")))
            self.track_lease()
            self.stats["ticks"] += 1
        self.watch()
        self.at(now + TICK_S + self.rng.uniform(0.0, 3.0), self.tick)

    def guard(self):
        now = self.clock.t
        self.gctl._LAST_LOGGED.clear()   # a new process every run
        self.bg.caller = "guard"
        dec = bloomgauge_lease_guard.guard(now=now, cfg=dict(self.cfg), client=self.bg, ctl=self.gctl,
                                           policy=self.gpol, alive=lambda _pid: self.daemon.alive, tick=self.gtk,
                                           alert=lambda key, msg: self.truth.alerts.append((now, key, msg)))
        self.bg.caller = "queue"
        self.stats["guard_runs"] += 1
        if dec is not None and dec.get("action") == "mgr_on":
            self.stats["guard_acts"] += 1
        if dec is not None and dec.get("action") == "yield":
            self.stats["guard_yields"] += 1
        self.track_lease()
        self.at(now + GUARD_EVERY_S, self.guard)

    def track_lease(self):
        """Keep truth.spans: a lease (real file, readable) from its since until it is seen gone; a corrupt
        file keeps the span open (its since is only the file's mtime)."""
        lease = self.daemon.ctl.lease_load(self.cfg["lease_path"])
        spans = self.truth.spans
        held = spans[-1] if spans and spans[-1][1] is None else None
        if lease is None:
            if held is not None:
                held[1] = self.clock.t
            return
        if lease.get("corrupt"):
            if held is None:
                spans.append([lease.get("since"), None])
            return
        if lease.get("yielded_at") is not None and not getattr(self, "_kept", None) == lease.get("yielded_at"):
            self._kept = lease.get("yielded_at")
            self.stats["kept_yields"] += 1
        if held is not None and held[0] != lease.get("since"):
            held[1] = self.clock.t
            held = None
        if held is None:
            spans.append([lease.get("since"), None])

    # -- online checks
    def healthy(self):
        bg = self.bg
        return not bg.down and bg.agent_on and not bg.log_bad and bg.can_enable and bg.fail_p == 0.0 \
            and not bg.switching()

    def owns_off(self):
        m = self.truth.mgr
        return (not self.bg.manager_on) and bool(m) and m[-1][2] in ("queue", "guard") and not m[-1][1]

    def quiet_since(self, span):
        now = self.clock.t
        return not any(now - t < span for t, _seen, _w, _who in self.truth.manual)

    def watch(self):
        """Every tick: a Manager the queue turned off must come back on in bounded healthy idle time."""
        now = self.clock.t
        cfg = self.cfg
        dt = TICK_S
        busy = bool(self.jobs)
        if busy and self.daemon.alive:
            self.stats["busy_s"] += dt
            if not self.bg.manager_on:
                self.stats["busy_mgr_off_s"] += dt
        hb_bound = cfg["idle_s"] + HOUR + 900.0 + 120.0
        guard_bound = cfg["lease_ttl_s"] + GUARD_EVERY_S + 120.0
        ok = self.owns_off() and self.healthy() and self.quiet_since(cfg["yield_s"] + hb_bound)
        if ok and self.daemon.alive and not busy:
            self.hb_run += dt
            self.max_hb = max(self.max_hb, self.hb_run)
            if self.hb_run > hb_bound:
                self.fail("handback", f"Manager off by the queue for {self.hb_run:.0f}s of healthy queue idle "
                                      f"(bound {hb_bound:.0f}s)")
                self.hb_run = -1e12   # once per spell
        else:
            self.hb_run = 0.0
        has_lease = Path(cfg["lease_path"]).exists()
        if ok and not self.daemon.alive and has_lease:
            self.guard_run += dt
            if self.guard_run > guard_bound:
                self.fail("handback", f"daemon dead, Manager off by the queue for {self.guard_run:.0f}s of healthy "
                                      f"time; the guard did not hand back (bound {guard_bound:.0f}s)")
                self.guard_run = -1e12
        else:
            self.guard_run = 0.0

    def fail(self, kind, msg):
        self.violations.append((self.clock.t, kind, msg))

    # -- the run
    def run(self):
        r = self
        r.at(T0, r.tick)
        r.at(T0 + r.rng.uniform(0, GUARD_EVERY_S), r.guard)
        k = r.rates
        r.every(k["burst"] * HOUR, r.burst)
        r.every(k["owner_toggle"] * HOUR, r.owner_toggle)
        r.every(k["owner_pick"] * HOUR, r.owner_pick)
        r.every(k["agent_toggle"] * HOUR, r.agent_toggle)
        r.every(k["agent_pick"] * HOUR, r.agent_pick)
        r.every(k["recovery"] * HOUR, r.bg.app_recovery)
        r.every(k["mgr_switch"] * HOUR, r.bg.manager_switch)
        r.every(300.0, r.filler)
        r.every(k["restart"] * HOUR, r.daemon_restart)
        r.every(k["corrupt"] * HOUR, r.corrupt_lease)
        r.spell(k["blocked"] * HOUR, 300, 3600, r.block_on, r.block_off)
        r.spell(k["app_down"] * HOUR, 60, 900, r.app_down, r.app_up)
        r.spell(k["agent_off"] * HOUR, 300, 3600, r.agent_off, r.agent_on)
        r.spell(k["log_bad"] * HOUR, 120, 5400, r.log_off, r.log_on)
        r.spell(k["fail"] * HOUR, 600, 5400, r.fail_on, r.fail_off)
        r.spell(k["death"] * HOUR, 300, 3 * HOUR, r.daemon_die, r.daemon_back)
        r.spell(k["mode"] * HOUR, 1800, 6 * HOUR, r.mode_away, r.mode_on)
        more = 0
        while r.heap:
            t, _s, fn, args = heapq.heappop(r.heap)
            if t > r.end:
                if not r.jobs or more >= 6:   # the quiet tail starts once the backlog has run
                    break
                r.end = t + sum(j[0] for j in r.jobs) + TAIL_S
                more += 1
            r.clock.t = t
            fn(*args)
        if r.truth.bad_log and r.truth.bad_log[-1][1] is None:
            r.truth.bad_log[-1][1] = r.end
        r.checks()
        return r

    # -- after the run
    def ledger(self):
        rows = []
        try:
            for ln in Path(self.cfg["ledger_path"]).read_text().splitlines():
                try:
                    rows.append(json.loads(ln))
                except ValueError:
                    pass
        except OSError:
            pass
        return rows

    def delivered(self, led):
        """[(t, key, new)] per alert raised. new is False when ollama-queue _control_alert wrote no row
        because an identical one (same key and msg) was still open: the owner already has that row.
        Rows close as escalation_index_janitor closes them, run every JANITOR_S by the escalation
        watcher: when control is not on, or when the newest mode-'on' result row of the key's kinds
        succeeded no earlier than the row less CONTROL_TS_SLACK_S; a log row also on the newest
        CONTROL_LOG_BACK row. (Until 2026-11-02 the sim closed a row only if the newest result at the
        next alert was ok, so a row the janitor had closed hours before looked open and the next
        streak's identical alert looked unsent.)"""
        rows = sorted((r["ts"], i, r) for i, r in enumerate(led)
                      if r.get("mode") == "on" and isinstance(r.get("ts"), (int, float)))
        kinds = {}
        for key, acts in escalation_index_janitor.CONTROL_KEYS.items():
            seq = [(ts, r.get("result") == "ok") for ts, _i, r in rows
                   if r.get("phase") == "result" and r.get("action") in acts]
            kinds[key] = ([t for t, _ok in seq], [ok for _t, ok in seq])
        back = [ts for ts, _i, r in rows if r.get("reason") == escalation_index_janitor.CONTROL_LOG_BACK]
        slack = escalation_index_janitor.CONTROL_TS_SLACK_S

        def mode_at(j):
            return next((m for s, e, m in self.truth.modes if s <= j < (float("inf") if e is None else e)), "on")

        def closes(rt, key, j):
            if mode_at(j) != "on":
                return True
            ts, oks = kinds.get(key, ([], []))
            k = bisect.bisect_right(ts, j)
            if k and oks[k - 1] and ts[k - 1] >= rt - slack:
                return True
            if key == escalation_index_janitor.CONTROL_LOG_KEY:
                k = bisect.bisect_right(back, j)
                return bool(k) and back[k - 1] >= rt - slack
            return False

        out, open_rows = [], []   # open_rows: [row t, key, msg, next janitor run]
        for t, key, msg in self.truth.alerts:
            still = []
            for row in open_rows:
                rt, k, _m, j = row
                while j <= t and not closes(rt, k, j):
                    j += JANITOR_S
                if j > t:
                    row[3] = j
                    still.append(row)
            open_rows = still
            new = not any(k == key and m == msg for _rt, k, m, _j in open_rows)
            if new:
                open_rows.append([t, key, msg, t + JANITOR_S - ((t - JANITOR_PHASE_S) % JANITOR_S)])
            out.append((t, key, new))
        return out

    def checks(self):
        cfg, tr = self.cfg, self.truth
        led = self.ledger()
        # rate: no 429 for the queue or the guard; own attempts within cfg per rolling hour
        for t, caller, what in tr.r429:
            self.violations.append((t, "rate", f"{caller} write ({what}) refused with 429"))
        for kind, acts, cap in (("Manager", ("mgr_off", "mgr_on"), cfg["mgr_per_h"]),
                                ("pick", ("pick",), cfg["picks_per_h"])):
            ts = sorted(r["ts"] for r in led if r.get("phase") == "attempt" and r.get("mode") == "on"
                        and r.get("action") in acts)
            for i, t in enumerate(ts):
                j = bisect.bisect_right(ts, t + HOUR - 1e-6)
                if j - i > cap:
                    self.violations.append((t, "rate", f"{j - i} own {kind} attempts within an hour (cap {cap})"))
                    break
        # owner: no write within yield_s after a manual change it could see; races counted
        ys = cfg["yield_s"]
        for t, caller, action, reached, log_bad, switching in tr.writes:
            if not reached:
                continue
            for tm, seen, what, who in tr.manual:
                if seen <= t < tm + ys:
                    self.violations.append((t, "owner", f"{caller} {action} {t - tm:.0f}s after a {who} {what} "
                                                         f"(row landed {seen - tm:.1f}s after it; yield_s {ys:.0f})"))
                elif tm <= t < seen:
                    self.races.append((t, caller, action, what))
            if log_bad:
                self.violations.append((t, "log", f"{caller} {action} while the activity log was unreadable"))
            if switching:
                self.violations.append((t, "log", f"{caller} {action} while BloomGauge was mid-change"))
        # mode: while control is off or shadow the queue only hands a real lease back (Manager on)
        for t, caller, action, reached, _lb, _sw in tr.writes:
            if caller == "queue" and action != "mgr_on":
                for s, e, mode in tr.modes:
                    if s <= t < (e if e is not None else self.end):
                        self.violations.append((t, "mode", f"queue {action} while control was {mode}"))
        # owner: a Manager the owner (or another agent) turned off is turned on again by the queue or the guard
        # only (a) after a new lease (a work cycle started after that off: handback 'always', by design), or
        # (b) as the hand-back of a lease held across that off, yield_s or more after it (the lease kept on a
        # yield, Fable review #8, by design)
        for t, on, who, why in tr.mgr:
            if not on or who not in ("queue", "guard"):
                continue
            prev = [m for m in tr.mgr if m[0] < t]
            if not prev:
                continue
            pt, pon, pwho, _pwhy = prev[-1]
            if pon or pwho not in ("owner", "agent"):
                continue
            if any(pt < since <= t for _lt, since in tr.leases if isinstance(since, (int, float))):
                continue
            held = any(isinstance(s, (int, float)) and s <= pt and (e is None or e >= t) for s, e in tr.spans)
            if held and t - pt >= ys:
                self.stats["kept_handbacks"] = self.stats.get("kept_handbacks", 0) + 1
                continue
            self.violations.append((t, "owner", f"{who} turned on a Manager the {pwho} turned off "
                                                 f"{t - pt:.0f}s earlier, with no work cycle since"
                                                 + (" (held lease, inside yield_s)" if held else "")))
        # handback at the end of the quiet tail
        if self.owns_off() and not any(t > tr.mgr[-1][0] for t, _s, _w, _who in tr.manual):
            # (a manual change after the queue's off, e.g. the owner's pick, leaves it off by choice)
            self.violations.append((self.end, "handback", "Manager still off by the queue at the end of the quiet tail"))
        # alerts: one per failure streak of ALERT_STREAK+ (per kind), at most one per unreadable-log spell
        streak_need = bloomgauge_policy.ALERT_STREAK
        raised = self.delivered(led)
        self.stats["alert_rows"] = sum(1 for _t, _k, new in raised if new)
        alerts = [(t, k) for t, k, new in raised if new]
        for key, acts in (("bloomgauge-control-manager", ("mgr_off", "mgr_on")), ("bloomgauge-control-pick", ("pick",))):
            res = sorted((r["ts"], i, r.get("result")) for i, r in enumerate(led)
                         if r.get("phase") == "result" and r.get("mode") == "on" and r.get("action") in acts)
            streaks, n, start = [], 0, None
            for ts, _i, result in res:
                if result == "failed":
                    n += 1
                    start = ts if n == 1 else start
                    if n == streak_need:
                        streaks.append([start, None])
                else:
                    if n >= streak_need:
                        streaks[-1][1] = ts
                    n = 0
            got = [t for t, k in alerts if k == key]
            seen = [t for t, k, _new in raised if k == key]   # a new row, or the identical row still open
            for s, e in streaks:
                inside = [t for t in got if t >= s and (e is None or t <= e)]
                if len(inside) > 1:
                    self.violations.append((s, "alerts", f"{key}: {len(inside)} alerts for one failure streak"))
                elif not any(t >= s and (e is None or t <= e) for t in seen):
                    self.violations.append((s, "alerts", f"{key}: 0 alerts for one failure streak"))
            if len(got) > len(streaks):
                self.violations.append((got[0], "alerts", f"{key}: {len(got)} alerts for {len(streaks)} streaks"))
        for s, e in tr.bad_log:
            n = sum(1 for t, k in alerts if k == bloomgauge_tick.LOG_ALERT_KEY and s <= t <= e + TICK_S * 2)
            if n > 1:
                self.violations.append((s, "alerts", f"{n} log alerts for one unreadable-log spell"))

    def report(self):
        st = self.stats
        own = [w for w in self.truth.writes if w[3]]
        busy_off = st["busy_mgr_off_s"] / st["busy_s"] if st["busy_s"] else 0.0
        return (f"{self.profile} seed {self.seed}: {self.days} d + {TAIL_S / 3600:.0f} h tail, {st['ticks']} ticks, {st['bursts']} bursts "
                f"({st['jobs']} jobs), {st['owner']} owner / {st['agent']} other-agent changes, {st['restarts']} restarts, "
                f"{st['deaths']} deaths, {len(own)} writes ({sum(1 for w in own if w[1] == 'guard')} by the guard, "
                f"{st['guard_yields']} guard yields), {st['kept_yields']} kept-lease yields, "
                f"{len(self.truth.alerts)} alerts ({st.get('alert_rows', 0)} rows), Manager off {busy_off:.0%} of busy time, longest healthy idle with "
                f"the queue's off {self.max_hb:.0f}s, {len(self.races)} unseeable races, "
                f"{len(self.violations)} violations")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=4.0)
    ap.add_argument("--seeds", default="1,2,3,4,5,6")
    ap.add_argument("--profile", default="normal,hostile", help="comma list of " + ", ".join(PROFILES))
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    bad = 0
    runs = [(p.strip(), int(s)) for p in a.profile.split(",") if p.strip() for s in a.seeds.split(",") if s.strip()]
    for profile, seed in runs:
        with tempfile.TemporaryDirectory(prefix="bgc-sim-") as home:
            sim = Sim(seed, a.days, home, a.verbose, profile).run()
            print(sim.report())
            for t, kind, msg in sim.violations[: (50 if a.verbose else 8)]:
                print(f"  FAIL {kind} @+{(t - T0) / 3600:.2f}h: {msg}")
            if a.verbose:
                for t, caller, action, what in sim.races[:20]:
                    print(f"  race @+{(t - T0) / 3600:.2f}h: {caller} {action} before the log row of a {what}")
            bad += len(sim.violations)
    print("PASS" if not bad else f"FAIL: {bad} violations")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
