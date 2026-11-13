"""sandbox-watch.py: fixture dirs only. No pages, no processes, no network; recovery commands are mocked.

    python3 -m unittest -q test_sandbox_watch      (run on claude-sandbox, never on the Mac)
"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sp = importlib.util.spec_from_file_location("sandbox_watch_t", HERE / "sandbox-watch.py")
W = importlib.util.module_from_spec(sp)
sp.loader.exec_module(W)
NOW = 1_800_000_000.0
HOST_A = "host-AAAA-secret"
HOST_B = "host-BBBB-secret"
OK_TAIL = ("darkbloom-sandbox info agent_starting host_id=x version=1.3.10\n"
           "darkbloom-sandbox info session_established session_id=u epoch=1 proven=true\n")


def finish_lines(times):
    return "".join('darkbloom-sandbox info worker_diagnostic worker=w line={"event":"job_finished","at":%s}\n' % t
                   for t in times)


class FakeOps:
    def __init__(self, cfg, term_works=True, comes_back=True):
        self.cfg, self.term_works, self.comes_back = cfg, term_works, comes_back
        self.calls, self.pid, self.termed = [], "100", False

    def agent_pid(self):
        if self.termed and self.term_works and "kick" not in self.calls:
            return None
        if "kick" in self.calls:
            return "200" if self.comes_back else None
        return self.pid

    def term_agent(self):
        self.calls.append("term")
        self.termed = True

    def kickstart(self):
        self.calls.append("kick")

    def sleep(self, s):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        r = Path(self.td.name)
        self.sb, self.dk = r / "sb", r / "dk"
        for d in (self.sb / "agent/controller", self.sb / "agent/state", self.dk, r / "nvme/images"):
            d.mkdir(parents=True)
        self.nvme = r / "nvme"
        self.cfg = W.Cfg(self.sb, self.dk, self.dk / "watch.state.json", self.dk / "maint.json")
        os.environ["INFRA_INCIDENTS_PATH"] = str(r / "inc.jsonl")
        os.environ["INFRA_INCIDENTS_SCREEN_LOCKED"] = "0"
        self.pages, self.recs, self.out = [], [], []
        self.write_all()

    # --- fixtures
    def write_status(self, **over):
        st = {"jobs_running": 0, "mode": "accepting", "session": "connected", "version": "1.3.10",
              "updated_at": int(NOW) - 5,
              "slots": {"linux": {"free": 4, "total": 4}, "macos": {"free": 1, "total": 1}},
              "disks": [{"state": "online", "path": str(self.nvme)}],
              "workspaces": {"reason": None}}
        st.update(over)
        self.cfg.status.write_text(json.dumps(st))
        os.utime(self.cfg.status, (NOW - 5, NOW - 5))

    def write_log(self, text=OK_TAIL, finishes=None):
        if finishes is None:
            finishes = [NOW - 3600 * k for k in range(1, 30)]
        self.cfg.log.write_text(finish_lines(finishes) + text)

    def write_all(self, host=HOST_A, darkbloom="0.10.12"):
        self.write_status()
        self.write_log()
        (self.sb / "agent/process.json").write_text(json.dumps({"pid": os.getpid(), "version": "1.3.10"}))
        (self.sb / "agent/enroll.json").write_text(json.dumps({"host_id": host}))
        (self.sb / "agent/agent.json").write_text(json.dumps(
            {"disks": [{"images_root": str(self.nvme / "images")}]}))
        (self.dk / "provider-run.json").write_text(json.dumps({"version": darkbloom}))

    def tick(self, now=NOW, dry=False, ops=None):
        return W.tick(self.cfg, now=now, dry_run=dry, ops=ops, out=self.out.append,
                      notify=lambda t, m: self.pages.append((t, m)),
                      record=lambda kind, comp, **f: self.recs.append((kind, f)))

    def warm(self):
        """First tick stores the baselines (host hash, versions) with nothing wrong."""
        self.tick()
        self.assertEqual(self.pages, [])

    def titles(self):
        return [t for t, _ in self.pages]

    def state(self):
        return json.loads(self.cfg.state.read_text())


class Conditions(Base):
    def test_all_clear(self):
        self.warm()
        self.assertIn("all clear", self.out[-1] if "all clear" in self.out[-1] else self.out[0])
        self.assertEqual(self.recs, [])

    def test_slots_recovery_with_jobs_pages_after_refusing_to_fix(self):
        self.warm()
        self.write_status(jobs_running=1)
        self.write_log(OK_TAIL.replace("session_established", "x") +
                       "darkbloom-sandbox error slot_needs_recovery profile=macos_arm64 slot=0 disk=/d reason=recoveryRequired\n")
        self.tick()
        self.assertTrue(any("autofix: refused: jobs_running" in o for o in self.out))
        self.assertIn("Sandbox: Sandbox slots need recovery", self.titles())

    def test_zero_free_zero_jobs_needs_10_minutes(self):
        self.warm()
        self.write_status(slots={"linux": {"free": 0, "total": 4}, "macos": {"free": 0, "total": 1}})
        self.tick(NOW)
        self.assertEqual(self.pages, [])
        self.write_status(slots={"linux": {"free": 0, "total": 4}, "macos": {"free": 0, "total": 1}},
                          updated_at=int(NOW + 650))
        os.utime(self.cfg.status, (NOW + 650, NOW + 650))
        self.tick(NOW + 650)
        self.assertEqual(self.titles(), ["Sandbox: Sandbox has no free slots"])

    def test_zero_free_with_a_job_running_is_fine(self):
        self.warm()
        for t in (NOW, NOW + 700):
            self.write_status(jobs_running=1, slots={"linux": {"free": 0, "total": 4}, "macos": {"free": 0, "total": 1}})
            os.utime(self.cfg.status, (t, t))
            self.tick(t)
        self.assertEqual(self.pages, [])

    def test_host_id_change_pages_and_never_leaks_the_id(self):
        self.warm()
        (self.sb / "agent/enroll.json").write_text(json.dumps({"host_id": HOST_B}))
        self.tick()
        self.assertEqual(self.titles(), ["Sandbox: Sandbox host ID changed"])
        blob = json.dumps(self.pages) + json.dumps(self.recs) + "\n".join(self.out) + self.cfg.state.read_text()
        self.assertNotIn(HOST_A, blob)
        self.assertNotIn(HOST_B, blob)

    def test_host_id_first_run_is_baseline_only(self):
        self.tick()
        self.assertEqual(self.pages, [])
        self.assertIn("host_hash", self.state())

    def test_app_attest_failure_and_recovery_by_new_session(self):
        self.warm()
        bad = "darkbloom-sandbox error app_attest_key_unusable failures=3 error=x\n"
        self.write_log(OK_TAIL + bad)
        self.tick()
        self.assertEqual(self.titles(), ["Sandbox: Sandbox App Attest failing"])
        self.write_log(OK_TAIL + bad + "darkbloom-sandbox info session_established session_id=u epoch=2\n")
        self.tick(NOW + 60)
        self.assertEqual(self.titles()[-1], "Sandbox: cleared")

    def test_agent_process_down_after_grace(self):
        self.warm()
        (self.sb / "agent/process.json").write_text(json.dumps({"pid": 2 ** 22 + 12345}))
        self.tick(NOW)
        self.assertEqual(self.pages, [])
        self.write_status(updated_at=int(NOW + 200))
        os.utime(self.cfg.status, (NOW + 200, NOW + 200))
        self.tick(NOW + 200)
        self.assertEqual(self.titles(), ["Sandbox: Sandbox agent is down"])

    def test_status_stale(self):
        self.warm()
        self.tick(NOW + 5000)
        self.assertIn("Sandbox: Sandbox status.json is stale", self.titles())

    def test_disconnected_for_10_minutes(self):
        self.warm()
        for t in (NOW, NOW + 700):
            self.write_status(session="retrying", updated_at=int(t))
            os.utime(self.cfg.status, (t, t))
            self.tick(t)
        self.assertEqual(self.titles(), ["Sandbox: Sandbox agent is disconnected"])

    def test_work_disk_offline_from_log_and_from_missing_mount(self):
        self.warm()
        self.write_log(OK_TAIL + "darkbloom-sandbox error work_disk_offline disk=/Volumes/X reason=missing\n")
        self.tick()
        self.assertEqual(self.titles(), ["Sandbox: Sandbox image disk offline"])
        self.write_log()
        os.rmdir(self.nvme / "images")
        os.rmdir(self.nvme)
        self.pages.clear()
        self.setUp_state_fresh()
        self.tick()
        self.assertEqual(self.titles(), ["Sandbox: Sandbox image disk offline"])

    def setUp_state_fresh(self):
        self.cfg.state.unlink()

    def test_no_job_for_well_past_the_usual_gap(self):
        self.warm()
        self.write_log(finishes=[NOW - 3600 * k - 30 * 3600 for k in range(0, 30)])   # hourly, last one 30 h ago
        self.tick()
        self.assertEqual(self.titles(), ["Sandbox: No sandbox job for a long time"])

    def test_no_job_alert_needs_enough_history(self):
        self.warm()
        self.write_log(finishes=[NOW - 100 * 3600])
        self.tick()
        self.assertEqual(self.pages, [])

    def test_no_job_alert_not_while_paused(self):
        self.warm()
        self.write_status(mode="paused")
        self.write_log(finishes=[NOW - 3600 * k - 30 * 3600 for k in range(0, 30)])
        self.tick()
        self.assertEqual(self.pages, [])

    def test_usual_gap_uses_run_records(self):
        week = [NOW - 7 * 86400 * k for k in range(12)]
        self.assertGreater(W.usual_gap(week), 7 * 86400)
        self.assertIsNone(W.usual_gap(week[:5]))


class Suppression(Base):
    def stuck_status(self, t=NOW):
        self.write_status(slots={"linux": {"free": 0, "total": 4}, "macos": {"free": 0, "total": 1}},
                          updated_at=int(t))
        os.utime(self.cfg.status, (t, t))

    def go_stuck(self):
        self.stuck_status(NOW)
        self.tick(NOW)
        self.stuck_status(NOW + 700)
        self.tick(NOW + 700)

    def test_marker_holds_alerts_but_logs_them(self):
        self.warm()
        W.set_marker(self.cfg, 3600, "model switch", "claude", NOW)
        self.go_stuck()
        self.assertEqual(self.pages, [])
        self.assertEqual([k for k, _ in self.recs], ["sandbox-alert-suppressed"])
        self.assertIn("maintenance", self.recs[0][1]["reason"])
        self.stuck_status(NOW + 760)
        self.tick(NOW + 760)
        self.assertEqual(len(self.recs), 1)          # logged once per episode, not every minute

    def test_expired_marker_no_longer_holds(self):
        self.warm()
        W.set_marker(self.cfg, 100, "x", "claude", NOW - 5000)
        self.go_stuck()
        self.assertEqual(len(self.pages), 1)

    def test_condition_pages_after_marker_ends(self):
        self.warm()
        W.set_marker(self.cfg, 800, "x", "claude", NOW)
        self.go_stuck()
        self.assertEqual(self.pages, [])
        self.stuck_status(NOW + 900)
        self.tick(NOW + 900)
        self.assertEqual(len(self.pages), 1)

    def test_version_change_grace_30_minutes(self):
        self.warm()
        (self.dk / "provider-run.json").write_text(json.dumps({"version": "0.10.13"}))
        self.tick(NOW + 1)
        self.stuck_status(NOW + 2)
        self.tick(NOW + 2)
        self.stuck_status(NOW + 700)
        self.tick(NOW + 700)
        self.assertEqual(self.pages, [])
        self.assertTrue(any("version-change grace" in f["reason"] for _, f in self.recs))
        t = NOW + 1 + 31 * 60
        self.stuck_status(t)
        self.tick(t)
        self.assertEqual(len(self.pages), 1)

    def test_sandbox_version_change_also_graces(self):
        self.warm()
        self.write_status(version="1.3.11")
        self.tick(NOW + 1)
        self.assertEqual(self.state()["version_changed_what"], "sandbox")

    def test_first_run_has_no_grace(self):
        self.tick()
        self.assertNotIn("version_changed_at", self.state())

    def test_memory_fit_never_pages(self):
        self.warm()
        self.write_log(OK_TAIL + "darkbloom-sandbox error job_refused reason=not enough free memory for the model\n")
        self.go_stuck()
        self.assertEqual(self.pages, [])
        self.assertTrue(any("memory" in f["reason"] for _, f in self.recs))

    def test_memory_fit_from_status_reason(self):
        self.warm()
        self.write_status(workspaces={"reason": "insufficient memory"},
                          slots={"linux": {"free": 0, "total": 4}, "macos": {"free": 0, "total": 1}})
        os.utime(self.cfg.status, (NOW + 700, NOW + 700))
        self.write_status(workspaces={"reason": "insufficient memory"}, updated_at=int(NOW + 700),
                          slots={"linux": {"free": 0, "total": 4}, "macos": {"free": 0, "total": 1}})
        os.utime(self.cfg.status, (NOW + 700, NOW + 700))
        self.tick(NOW)
        self.tick(NOW + 700)
        self.assertEqual(self.pages, [])

    def test_dry_run_does_not_log_suppressions(self):
        self.warm()
        W.set_marker(self.cfg, 3600, "x", "claude", NOW)
        self.stuck_status(NOW)
        self.tick(NOW, dry=True)
        self.assertEqual(self.recs, [])


class Paging(Base):
    def go(self, t):
        self.write_status(slots={"linux": {"free": 0, "total": 4}, "macos": {"free": 0, "total": 1}},
                          updated_at=int(t))
        os.utime(self.cfg.status, (t, t))
        self.tick(t)

    def test_one_page_until_clear_repeat_after_6h_and_one_cleared_note(self):
        self.warm()
        self.go(NOW)
        self.go(NOW + 700)
        self.assertEqual(len(self.pages), 1)
        self.go(NOW + 1400)
        self.go(NOW + 3 * 3600)
        self.assertEqual(len(self.pages), 1)
        self.go(NOW + 7 * 3600)
        self.assertEqual(len(self.pages), 2)                    # repeat after 6 h
        self.write_log(finishes=[NOW + 8 * 3600 - 3600 * k for k in range(1, 30)])
        self.write_status(updated_at=int(NOW + 8 * 3600))
        os.utime(self.cfg.status, (NOW + 8 * 3600, NOW + 8 * 3600))
        self.tick(NOW + 8 * 3600)
        self.assertEqual(self.titles()[-1], "Sandbox: cleared")
        self.assertEqual(len(self.pages), 3)
        self.write_status(updated_at=int(NOW + 8 * 3600 + 60))
        os.utime(self.cfg.status, (NOW + 8 * 3600 + 60, NOW + 8 * 3600 + 60))
        self.tick(NOW + 8 * 3600 + 60)
        self.assertEqual(len(self.pages), 3)                    # only one cleared note

    def test_no_cleared_note_for_a_condition_that_never_paged(self):
        self.warm()
        self.go(NOW)                                            # stuck but < 10 min: not paged
        self.write_status(updated_at=int(NOW + 60))
        os.utime(self.cfg.status, (NOW + 60, NOW + 60))
        self.tick(NOW + 60)
        self.assertEqual(self.pages, [])

    def test_cleared_note_held_while_marker_set(self):
        self.warm()
        self.go(NOW)
        self.go(NOW + 700)
        W.set_marker(self.cfg, 3600, "x", "claude", NOW + 800)
        self.write_status(updated_at=int(NOW + 900))
        os.utime(self.cfg.status, (NOW + 900, NOW + 900))
        self.tick(NOW + 900)
        self.assertEqual(len(self.pages), 1)


RECOVERY_LOG = (OK_TAIL.replace("session_established", "x") +
                "darkbloom-sandbox error slot_needs_recovery profile=macos_arm64 slot=0 disk=/d reason=recoveryRequired\n"
                "darkbloom-sandbox error slot_needs_recovery profile=linux_arm64 slot=1 disk=/d reason=recoveryRequired\n")


class AutoFix(Base):
    def stuck(self):
        self.write_log(RECOVERY_LOG)
        for d in ("macos-slot-0", "slot-1", "slot-0"):
            (self.cfg.controller / d).mkdir(parents=True, exist_ok=True)
            (self.cfg.controller / d / "runtime.snapshot").write_text("snap-" + d)

    def test_fix_runs_at_zero_jobs_moves_only_named_snapshots_and_sets_marker(self):
        self.warm()
        self.stuck()
        ops = FakeOps(self.cfg)
        self.tick(ops=ops)
        self.assertEqual(ops.calls, ["term", "kick"])
        self.assertFalse((self.cfg.controller / "macos-slot-0/runtime.snapshot").exists())
        self.assertFalse((self.cfg.controller / "slot-1/runtime.snapshot").exists())
        self.assertTrue((self.cfg.controller / "slot-0/runtime.snapshot").exists())   # not named: untouched
        backups = list(self.sb.glob("recovery-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "macos-slot-0/runtime.snapshot").read_text(), "snap-macos-slot-0")
        self.assertIsNotNone(W.read_marker(self.cfg, NOW + 1))
        self.assertEqual(self.pages, [])                                               # success: no page
        self.assertEqual(self.recs[0][0], "sandbox-autofix")
        self.assertTrue(self.recs[0][1]["ok"])
        self.assertEqual(len(self.state()["fixes"]), 1)

    def test_refuses_when_a_job_is_running(self):
        self.warm()
        self.stuck()
        self.write_status(jobs_running=1)
        ops = FakeOps(self.cfg)
        self.tick(ops=ops)
        self.assertEqual(ops.calls, [])
        self.assertTrue((self.cfg.controller / "slot-1/runtime.snapshot").exists())

    def test_refuses_when_status_is_stale(self):
        self.warm()
        self.stuck()
        os.utime(self.cfg.status, (NOW - 500, NOW - 500))
        self.write_status(updated_at=int(NOW - 500))
        os.utime(self.cfg.status, (NOW - 500, NOW - 500))
        ops = FakeOps(self.cfg)
        self.tick(ops=ops)
        self.assertEqual(ops.calls, [])

    def test_refuses_when_jobs_field_unreadable(self):
        self.warm()
        self.stuck()
        st = json.loads(self.cfg.status.read_text())
        del st["jobs_running"]
        self.cfg.status.write_text(json.dumps(st))
        os.utime(self.cfg.status, (NOW - 5, NOW - 5))
        ops = FakeOps(self.cfg)
        self.tick(ops=ops)
        self.assertEqual(ops.calls, [])

    def test_two_tries_a_day_then_page_the_limit(self):
        self.warm()
        for i, t in enumerate((NOW, NOW + 2400, NOW + 4800)):
            self.stuck()
            self.write_status(updated_at=int(t))
            os.utime(self.cfg.status, (t, t))
            W.clear_marker(self.cfg)
            ops = FakeOps(self.cfg)
            self.tick(t, ops=ops)
            self.assertEqual(ops.calls, ["term", "kick"] if i < 2 else [], i)
        self.assertEqual(len(self.state()["fixes"]), 2)
        self.assertIn("Sandbox: Sandbox slot auto-fix limit reached", self.titles())
        self.assertEqual(self.titles().count("Sandbox: Sandbox slot auto-fix limit reached"), 1)
        # the plain recovery page is folded into the limit page
        self.assertNotIn("Sandbox: Sandbox slots need recovery", self.titles())

    def test_tries_reset_after_24_hours(self):
        self.warm()
        self.stuck()
        self.tick(ops=FakeOps(self.cfg))
        t = NOW + 25 * 3600
        self.stuck()
        self.write_status(updated_at=int(t))
        os.utime(self.cfg.status, (t, t))
        W.clear_marker(self.cfg)
        ops = FakeOps(self.cfg)
        self.tick(t, ops=ops)
        self.assertEqual(ops.calls, ["term", "kick"])

    def test_failure_pages_and_releases_marker(self):
        self.warm()
        self.stuck()
        ops = FakeOps(self.cfg, comes_back=False)
        self.tick(ops=ops)
        self.assertIn("Sandbox: Sandbox slot auto-fix failed", self.titles())
        self.assertIsNone(W.read_marker(self.cfg, NOW + 1))
        self.assertFalse(self.recs[0][1]["ok"])

    def test_agent_that_will_not_stop_leaves_files_alone_and_pages(self):
        self.warm()
        self.stuck()
        ops = FakeOps(self.cfg, term_works=False)
        self.tick(ops=ops)
        self.assertEqual(ops.calls, ["term"])
        self.assertTrue((self.cfg.controller / "slot-1/runtime.snapshot").exists())
        self.assertIn("Sandbox: Sandbox slot auto-fix failed", self.titles())

    def test_launchd_restart_before_move_leaves_files_alone_and_pages(self):
        self.warm()
        self.stuck()
        ops = FakeOps(self.cfg)
        ops.agent_pid = lambda: "300" if ops.termed else ops.pid      # KeepAlive relaunch beat us to it
        self.tick(ops=ops)
        self.assertEqual(ops.calls, ["term"])
        self.assertTrue((self.cfg.controller / "slot-1/runtime.snapshot").exists())
        self.assertIn("Sandbox: Sandbox slot auto-fix failed", self.titles())
        self.assertIn("launchd restarted", self.recs[0][1]["detail"])

    def test_waits_30_min_between_tries(self):
        self.warm()
        self.stuck()
        self.tick(ops=FakeOps(self.cfg, comes_back=False))               # try 1 fails
        for t, want in ((NOW + 60, []), (NOW + 1700, []), (NOW + 1900, ["term", "kick"])):
            self.stuck()
            self.write_status(updated_at=int(t))
            os.utime(self.cfg.status, (t, t))
            ops = FakeOps(self.cfg)
            self.tick(t, ops=ops)
            self.assertEqual(ops.calls, want, t - NOW)

    def test_no_fix_while_someone_elses_marker_is_set(self):
        self.warm()
        self.stuck()
        W.set_marker(self.cfg, 600, "slots restart", "sandbox-slots", NOW)
        ops = FakeOps(self.cfg)
        self.tick(ops=ops)
        self.assertEqual(ops.calls, [])
        self.assertTrue(any("autofix: refused: maintenance marker" in o for o in self.out))

    def test_no_fix_during_version_grace(self):
        self.warm()
        self.stuck()
        (self.dk / "provider-run.json").write_text(json.dumps({"version": "0.10.13"}))
        ops = FakeOps(self.cfg)
        self.tick(ops=ops)
        self.assertEqual(ops.calls, [])

    def test_never_runs_darkbloom_setup(self):
        src = (HERE / "sandbox-watch.py").read_text()
        calls = [l for l in src.splitlines() if "subprocess.run(" in l]
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(any(x in l for x in ('"pgrep"', '"pkill"', '"launchctl"')) for l in calls))
        self.assertFalse(any("setup" in l for l in calls))

    def test_marker_holds_other_alerts_during_the_fix(self):
        self.warm()
        self.stuck()
        (self.sb / "agent/enroll.json").write_text(json.dumps({"host_id": HOST_B}))
        self.tick(ops=FakeOps(self.cfg))
        self.assertNotIn("Sandbox: Sandbox host ID changed", self.titles())


class DryRun(Base):
    def snapshot(self):
        out = {}
        for root in (self.sb, self.dk):
            for p in sorted(root.rglob("*")):
                if p.is_file():
                    out[str(p)] = (p.read_bytes(), p.stat().st_mtime)
        return out, sorted(Path(self.td.name).glob("inc.jsonl"))

    def test_dry_run_writes_nothing_pages_nothing_fixes_nothing(self):
        self.warm()
        self.cfg.state.unlink()
        self.write_log(RECOVERY_LOG)
        (self.cfg.controller / "slot-1").mkdir(exist_ok=True)
        (self.cfg.controller / "slot-1/runtime.snapshot").write_text("s")
        (self.sb / "agent/enroll.json").write_text(json.dumps({"host_id": HOST_B}))
        before = self.snapshot()
        ops = FakeOps(self.cfg)
        self.tick(dry=True, ops=ops)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.cfg.state.exists())
        self.assertFalse(self.cfg.marker.exists())
        self.assertEqual((self.pages, self.recs, ops.calls), ([], [], []))
        self.assertTrue(any("would run the slot recovery" in o for o in self.out))

    def test_dry_run_cli_set_maintenance_writes_nothing(self):
        W.main(["--dry-run", "--sandbox-dir", str(self.sb), "--darkbloom-dir", str(self.dk),
                "--state", str(self.dk / "s.json"), "--marker", str(self.dk / "m.json"), "--set-maintenance", "60"])
        self.assertFalse((self.dk / "m.json").exists())


class Marker(Base):
    def test_set_read_expire_clear(self):
        W.set_marker(self.cfg, 100, "r", "claude", NOW)
        self.assertEqual(W.read_marker(self.cfg, NOW + 50)["reason"], "r")
        self.assertIsNone(W.read_marker(self.cfg, NOW + 101))
        W.clear_marker(self.cfg)
        self.assertFalse(self.cfg.marker.exists())

    def test_set_never_shortens_and_cli_works(self):
        W.set_marker(self.cfg, 1000, "long", "a", NOW)
        W.set_marker(self.cfg, 10, "short", "b", NOW)
        self.assertGreaterEqual(float(json.loads(self.cfg.marker.read_text())["until"]), NOW + 1000)
        W.clear_marker(self.cfg)
        W.main(["--sandbox-dir", str(self.sb), "--darkbloom-dir", str(self.dk), "--marker", str(self.cfg.marker),
                "--set-maintenance", "120", "slots restart", "--owner", "sandbox-slots"])
        self.assertEqual(json.loads(self.cfg.marker.read_text())["owner"], "sandbox-slots")

    def test_garbage_marker_is_ignored(self):
        self.cfg.marker.write_text("not json")
        self.assertIsNone(W.read_marker(self.cfg, NOW))


if __name__ == "__main__":
    unittest.main()
