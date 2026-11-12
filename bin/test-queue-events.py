#!/usr/bin/env python3
"""Tests for queue-event-log.py (event production) and queue-events-deliver.py (targeted
delivery: hook, --watch, --inflight). Hermetic: every path is redirected into a temp dir.
Run: python3 ~/bin/test-queue-events.py"""
import fcntl, json, os, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LOGGER = os.path.join(HERE, "queue-event-log.py")
DELIVER = os.path.join(HERE, "queue-events-deliver.py")


class Env:
    def __init__(self):
        self.td = tempfile.mkdtemp(prefix="qe-test-")
        self.dir = os.path.join(self.td, "d")
        self.logs = os.path.join(self.td, "logs")
        self.socks = os.path.join(self.td, "socks")
        for d in (self.dir, self.logs, self.socks):
            os.makedirs(d)
        self.status = os.path.join(self.td, "status.txt")
        self.state = os.path.join(self.td, "state.json")
        self.set_status("")
        self.set_state([])

    def sock(self, pid, alive=True):
        p = os.path.join(self.socks, f"{pid}.sock")
        if alive:
            open(p, "w").close()
        return p

    def set_status(self, text):
        with open(self.status, "w") as f:
            f.write(text)

    def set_state(self, jobs):
        with open(self.state, "w") as f:
            f.write(json.dumps({"jobs": jobs}))

    def sidecar(self, jid, **kw):
        rec = {"id": jid, "label": f"lab-{jid}", "status": "done"}
        rec.update(kw)
        with open(os.path.join(self.logs, f"{jid}.done.json"), "w") as f:
            f.write(json.dumps(rec))

    def env(self, **kw):
        e = {k: v for k, v in os.environ.items()
             if not k.startswith(("CLAUDE_CODE_", "CLAUDE_PID", "QEL_", "QEW_"))}
        e.update(QEL_DIR=self.dir, QEL_LOG_DIR=self.logs, QEL_STATE_PATH=self.state,
                 QEL_STATUS_CMD=f"cat {self.status}")
        e.update(kw)
        return e

    def poll(self):
        r = subprocess.run([sys.executable, LOGGER, "--once"], env=self.env(),
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        return int(r.stdout.strip())

    def events(self):
        try:
            with open(os.path.join(self.dir, "queue-events.jsonl")) as f:
                return [json.loads(l) for l in f]
        except OSError:
            return []

    def hook(self, sid, sock, bridge=None, extra=None, **envkw):
        kw = {"CLAUDE_CODE_MESSAGING_SOCKET": sock} if sock else {}
        if bridge:
            kw["CLAUDE_CODE_BRIDGE_SESSION_ID"] = bridge
        kw.update(envkw)
        inp = {"session_id": sid, "hook_event_name": "PostToolUse"}
        inp.update(extra or {})
        r = subprocess.run([sys.executable, DELIVER], input=json.dumps(inp), env=self.env(**kw),
            capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        if not r.stdout.strip():
            return ""
        return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


def row(status, jid, label, extra=""):
    return f"[{status:8s}] {jid}  {label:28s} m  host=x pid=None  exit=None{extra}\n"


class LoggerTests(unittest.TestCase):
    def test_done_sidecar_becomes_passed_event_without_history_flood(self):
        E = Env()
        E.sidecar("old1", runner_verdict="PASS")
        self.assertEqual(E.poll(), 0)                   # first run: baseline only
        me = E.sock(4242)
        E.sidecar("aaa111", runner_verdict="PASS", launched_by="uds:" + me,
                  launched_by_session="session_X", bundle="b1")
        self.assertEqual(E.poll(), 1)
        ev = E.events()[-1]
        self.assertEqual((ev["id"], ev["state"], ev["verdict"]), ("aaa111", "passed", "PASS"))
        self.assertEqual((ev["launched_by"], ev["launched_by_session"], ev["bundle"]),
                         (me, "session_X", "b1"))
        self.assertEqual(E.poll(), 0)                   # no re-emit
        E.sidecar("aaa111", runner_verdict="PASS", launched_by=me)   # rewrite: still no dup
        self.assertEqual(E.poll(), 0)

    def test_failed_seen_by_status_and_sidecar_is_one_event(self):
        E = Env()
        E.poll()
        E.set_status(row("failed", "bbb222", "x-fix", "  verdict=FAIL (mode=x)"))
        E.set_state([{"id": "bbb222", "status": "failed", "launched_by": "/tmp/cc-socks/1.sock"}])
        self.assertEqual(E.poll(), 1)
        E.sidecar("bbb222", status="failed", runner_verdict=None)
        self.assertEqual(E.poll(), 0)
        ev = E.events()[-1]
        self.assertEqual((ev["state"], ev["launched_by"]), ("failed", "/tmp/cc-socks/1.sock"))
        snap = json.load(open(os.path.join(E.dir, "queue-events.failed.json")))
        self.assertEqual(snap["failed"][0]["launched_by"], "/tmp/cc-socks/1.sock")

    def test_verdict_upgrade_still_emits(self):
        E = Env()
        E.poll()
        E.set_status(row("failed", "ccc333", "y", "  verdict=PENDING"))
        self.assertEqual(E.poll(), 1)
        E.set_status(row("failed", "ccc333", "y", "  verdict=FAIL"))
        self.assertEqual(E.poll(), 1)

    def test_gate_nonpass_row_and_needs_info_pause(self):
        E = Env()
        np = os.path.join(E.logs, "NON-PASS-GATES.md")
        open(np, "w").write("- `old` **fail** code_high=1 - t - `g`\n")
        E.poll()
        E.sidecar("ddd444", runner_verdict="PASS", launched_by="/tmp/cc-socks/7.sock")
        E.poll()
        with open(np, "a") as f:
            f.write("- `ddd444` **concerns** code_high=0 - t - `g.json` | code: off-by-one\n")
        E.set_status(row("paused", "eee555", "z"))
        E.set_state([{"id": "eee555", "status": "paused", "pause_reason": "needs_info"}])
        self.assertEqual(E.poll(), 2)
        by = {e["state"]: e for e in E.events()}
        g = by["gate_nonpass"]
        self.assertEqual((g["id"], g["label"], g["verdict"], g["reason"], g["launched_by"]),
                         ("ddd444", "lab-ddd444", "concerns code_high=0", "code: off-by-one",
                          "/tmp/cc-socks/7.sock"))
        self.assertEqual(by["needs_info"]["id"], "eee555")
        self.assertEqual(E.poll(), 0)

    def test_lost_state_never_reuses_seq(self):
        E = Env()
        E.poll()
        E.sidecar("fff666")
        E.poll()
        top = E.events()[-1]["seq"]
        os.unlink(os.path.join(E.dir, "queue-events.state.json"))
        E.poll()                                        # re-baseline
        E.sidecar("ggg777")
        E.poll()
        self.assertGreater(E.events()[-1]["seq"], top)


    def test_requeued_job_reemits_its_next_outcome(self):
        E = Env()
        E.poll()
        E.set_status(row("failed", "hhh111", "h", "  verdict=FAIL"))
        self.assertEqual(E.poll(), 1)
        self.assertEqual(E.poll(), 0)                   # unchanged: no repeat
        E.set_status(row("pending", "hhh111", "h"))    # requeued / resumed in place
        self.assertEqual(E.poll(), 0)
        E.set_status(row("running", "hhh111", "h"))
        self.assertEqual(E.poll(), 0)
        E.set_status(row("failed", "hhh111", "h", "  verdict=FAIL"))
        self.assertEqual(E.poll(), 1)                   # the second failure is news
        self.assertEqual([e["state"] for e in E.events() if e["id"] == "hhh111"],
                         ["failed", "failed"])

    def test_stale_running_row_never_rearms_a_duplicate(self):
        E = Env()
        E.poll()
        E.set_status(row("running", "iii222", "i"))
        self.assertEqual(E.poll(), 0)
        E.sidecar("iii222", runner_verdict="PASS")     # finished; status read still stale
        self.assertEqual(E.poll(), 1)
        self.assertEqual(E.poll(), 0)                   # still "running": no re-arm
        E.set_status(row("done", "iii222", "i", "  verdict=PASS"))
        self.assertEqual(E.poll(), 0)
        self.assertEqual(len([e for e in E.events() if e["id"] == "iii222"]), 1)

    def test_needs_info_asked_again_after_the_answer(self):
        E = Env()
        E.poll()
        ask = [{"id": "jjj333", "status": "paused", "pause_reason": "needs_info"}]
        E.set_status(row("paused", "jjj333", "j"))
        E.set_state(ask)
        self.assertEqual(E.poll(), 1)
        self.assertEqual(E.poll(), 0)
        E.set_status(row("pending", "jjj333", "j"))    # answered -> resumed
        E.set_state([{"id": "jjj333", "status": "pending"}])
        self.assertEqual(E.poll(), 0)
        E.set_status(row("paused", "jjj333", "j"))     # a second question
        E.set_state(ask)
        self.assertEqual(E.poll(), 1)
        self.assertEqual([e["state"] for e in E.events() if e["id"] == "jjj333"],
                         ["needs_info", "needs_info"])

    def test_rewritten_sidecar_reemits_only_after_a_requeue(self):
        E = Env()
        E.poll()
        p = os.path.join(E.logs, "kkk444.done.json")

        def rewrite():
            E.sidecar("kkk444", status="failed")
            t = os.stat(p).st_mtime + 5
            os.utime(p, (t, t))

        E.sidecar("kkk444", status="failed")
        self.assertEqual(E.poll(), 1)
        rewrite()                                       # same outcome re-frozen (regate)
        self.assertEqual(E.poll(), 0)
        E.set_status(row("pending", "kkk444", "k"))    # requeued: listed again
        self.assertEqual(E.poll(), 0)
        E.set_status("")                                # ran, failed, pruned between polls
        rewrite()
        self.assertEqual(E.poll(), 1)
        self.assertEqual([e["state"] for e in E.events() if e["id"] == "kkk444"],
                         ["failed", "failed"])


class DeliverTests(unittest.TestCase):
    def setUp(self):
        self.E = E = Env()
        E.poll()
        self.me, self.other = E.sock(1001), E.sock(2002)
        self.gone = E.sock(3003, alive=False)
        E.hook("sA", self.me)                           # sessions exist before the events
        E.hook("sB", self.other)
        E.sidecar("j1", runner_verdict="PASS", launched_by=self.me)
        E.sidecar("j2", status="failed", launched_by=self.other)
        E.sidecar("j3", status="failed", launched_by=self.gone)
        E.sidecar("j4")                                 # unstamped (cron/daemon)
        self.assertEqual(E.poll(), 4)

    def test_targeting(self):
        a = self.E.hook("sA", self.me)
        self.assertIn("passed (yours): lab-j1 [j1]", a)
        self.assertNotIn("[j2]", a)                     # another live session's job
        self.assertIn("(launcher gone): lab-j3", a)
        self.assertIn("(no launcher): lab-j4", a)
        b = self.E.hook("sB", self.other)
        self.assertIn("failed (yours): lab-j2", b)
        self.assertNotIn("[j1]", b)
        self.assertNotIn("[j3]", b)                     # unowned: shown once, already shown
        self.assertEqual(self.E.hook("sA", self.me), "")

    def test_clear_resumes_from_process_cursor(self):
        self.E.hook("sA", self.me)
        self.E.sidecar("j5", runner_verdict="PASS", launched_by=self.me)
        self.E.poll()
        a2 = self.E.hook("sA2", self.me)                # /clear: new sid, same process
        self.assertIn("lab-j5 [j5]", a2)
        self.assertNotIn("[j1]", a2)

    def test_pid_reuse_is_not_yours(self):
        self.E.sidecar("j6", runner_verdict="PASS", launched_by=self.me,
                       launched_by_session="session_OLD")
        self.E.poll()
        a = self.E.hook("sA", self.me, bridge="session_NEW")
        self.assertIn("(launcher gone): lab-j6", a)

    def test_watch_streams_shares_cursor_and_exits_when_idle(self):
        E = self.E
        E.set_state([{"id": "j9", "status": "running", "launched_by": self.me}])
        env = E.env(CLAUDE_CODE_MESSAGING_SOCKET=self.me, CLAUDE_CODE_SESSION_ID="sA",
                    QEW_IDLE_EXIT_S="1", QEW_POLL_S="0.2")
        p = subprocess.Popen([sys.executable, DELIVER, "--watch"], env=env,
                             stdout=subprocess.PIPE, text=True)
        first = p.stdout.readline()
        self.assertIn("passed (yours): lab-j1", first)
        E.sidecar("j9", runner_verdict="PASS", launched_by=self.me)
        E.poll()
        got = ""
        t0 = time.time()
        while "[j9]" not in got and time.time() - t0 < 10:
            got += p.stdout.readline()
        self.assertIn("queue #", got)
        self.assertIn("lab-j9 [j9]", got)
        E.set_state([])                                 # nothing in flight -> exits
        self.assertEqual(p.wait(timeout=30), 0)
        self.assertEqual(E.hook("sA", self.me), "")     # already delivered by the watcher

    def test_inflight_counts_own_jobs_and_gate_children(self):
        E = self.E
        E.sidecar("abcdef123456", launched_by=self.me)      # pruned parent of a gate row
        E.set_state([{"id": "k1", "status": "pending", "launched_by": "uds:" + self.me},
                     {"id": "k2", "status": "running", "launched_by": self.other},
                     {"id": "k3", "status": "failed", "launched_by": self.me},
                     {"id": "k4", "status": "pending", "label": "gate-abcdef123456"},
                     {"id": "k5", "status": "pending", "label": "gate-feedbeef0000"}])
        r = subprocess.run([sys.executable, DELIVER, "--inflight"], text=True, capture_output=True,
                           env=E.env(CLAUDE_CODE_MESSAGING_SOCKET=self.me))
        self.assertEqual(r.stdout.strip(), "2")


    def test_old_watcher_sid_and_new_hook_share_the_process_cursor(self):
        E = self.E
        E.hook("sA", self.me)
        E.sidecar("j7", runner_verdict="PASS", launched_by=self.me)
        E.poll()
        old = E.hook("sOld", self.me)                   # watcher armed before /clear
        self.assertIn("lab-j7 [j7]", old)
        self.assertEqual(E.hook("sA", self.me), "")     # same process: not shown twice

    def test_subagent_hook_call_delivers_and_consumes_nothing(self):
        E = self.E
        E.hook("sA", self.me)
        E.sidecar("j8", status="failed", launched_by=self.me)
        E.poll()
        sdir = os.path.join(E.dir, "queue-events.sessions")
        before = {f: open(os.path.join(sdir, f)).read() for f in os.listdir(sdir)}
        self.assertEqual(E.hook("sA", self.me, extra={"agent_id": "a1b2", "agent_type": "scout"}), "")
        after = {f: open(os.path.join(sdir, f)).read() for f in os.listdir(sdir)}
        self.assertEqual(before, after)
        self.assertIn("failed (yours): lab-j8 [j8]", E.hook("sA", self.me))

    def test_held_lock_delays_but_never_blocks_delivery(self):
        E = self.E
        with open(os.path.join(E.dir, "queue-events.lock"), "a") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            t0 = time.time()
            a = E.hook("sA", self.me, QEW_LOCK_WAIT_S="0.6")
            took = time.time() - t0
        self.assertIn("passed (yours): lab-j1", a)      # fail-open: delivered anyway
        self.assertGreaterEqual(took, 0.5)              # ...after honouring the lock
        self.assertEqual(E.hook("sA", self.me), "")

    def test_watch_without_session_id_keeps_no_shared_session_file(self):
        E = self.E
        env = E.env(CLAUDE_CODE_MESSAGING_SOCKET=self.me, QEW_IDLE_EXIT_S="0.5", QEW_POLL_S="0.2")
        r = subprocess.run([sys.executable, DELIVER, "--watch"], env=env, capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("passed (yours): lab-j1", r.stdout)
        sdir = os.path.join(E.dir, "queue-events.sessions")
        self.assertNotIn("x", os.listdir(sdir))
        self.assertIn("sock-1001", os.listdir(sdir))
        self.assertEqual(E.hook("sNew", self.me).count("[j1]"), 0)   # /clear: process cursor


if __name__ == "__main__":
    unittest.main(verbosity=1)
