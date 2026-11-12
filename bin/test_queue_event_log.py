import json, os, subprocess, sys, tempfile, unittest

BIN = os.path.expanduser("~/bin")
LOG = os.path.join(BIN, "queue-event-log.py")
DELIVER = os.path.join(BIN, "queue-events-deliver.py")


class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.status = os.path.join(self.d, "status.txt")
        # Hermetic: no real session socket/env, no real sidecars or queue state.
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("CLAUDE_CODE_", "CLAUDE_PID", "QEL_", "QEW_"))}
        os.makedirs(os.path.join(self.d, "logs"))
        self.env.update(QEL_DIR=self.d, QEL_STATUS_CMD=f"cat {self.status}",
                        QEL_LOG_DIR=os.path.join(self.d, "logs"),
                        QEL_STATE_PATH=os.path.join(self.d, "state.json"))
        self.set([])

    def set(self, rows):
        open(self.status, "w").write("\n".join(
            f"[{s:8s}] {i}  {l:28s} qwen host=x pid=1  exit=0{extra}" for i, l, s, extra in rows) + "\n")

    def poll(self):
        return int(subprocess.run([sys.executable, LOG, "--once"], env=self.env, capture_output=True, text=True).stdout)

    def events(self):
        p = os.path.join(self.d, "queue-events.jsonl")
        return [json.loads(l) for l in open(p)] if os.path.exists(p) else []

    def hook(self, sid, ev="PostToolUse"):
        r = subprocess.run([sys.executable, DELIVER], env=self.env, input=json.dumps(
            {"session_id": sid, "hook_event_name": ev}), capture_output=True, text=True)
        return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"] if r.stdout.strip() else ""

    def test_transitions_and_no_dup(self):
        self.poll()  # baseline
        self.set([("aaa111", "job-a", "running", "")]); self.assertEqual(self.poll(), 0)
        self.set([("aaa111", "job-a", "failed", "  reason=boom")]); self.assertEqual(self.poll(), 1)
        self.assertEqual(self.poll(), 0)  # unchanged -> no dup
        e = self.events()[0]
        self.assertEqual((e["seq"], e["id"], e["state"], e["reason"]), (1, "aaa111", "failed", "boom"))
        self.set([("aaa111", "job-a", "done", "  verdict=PASS")]); self.assertEqual(self.poll(), 1)
        self.assertEqual(self.events()[-1]["state"], "passed")
        self.set([]); self.poll(); self.assertEqual(len(self.events()), 2)

    def test_needs_review_state(self):
        self.poll()
        self.set([("nnn111", "job-n", "done", "  verdict=NEEDS-REVIEW (soft: fix_hunks)")]); self.assertEqual(self.poll(), 1)
        e = self.events()[-1]
        self.assertEqual((e["state"], e["verdict"]), ("needs-review", "NEEDS-REVIEW (soft: fix_hunks)"))
        self.assertEqual(self.poll(), 0)  # unchanged -> no dup
        self.assertIn("needs-review (no launcher): job-n", self.hook("A"))

    def test_rotation(self):
        self.env["QEL_MAX_BYTES"] = "10"
        self.poll()
        for n in range(3):
            self.set([(f"b{n}0000", "j", "blocked", "")]); self.poll()
        self.assertTrue(os.path.exists(os.path.join(self.d, "queue-events.jsonl.1")))

    def test_cursor_handoff_and_new_session(self):
        self.poll()
        self.set([("ccc111", "job-c", "blocked", "")]); self.poll()
        a = self.hook("A")                       # new session sees event nobody was shown
        self.assertIn("blocked (no launcher): job-c", a)
        self.assertEqual(self.hook("A"), "")     # no repeat to same session
        self.assertEqual(self.hook("B"), "")     # B starts at global cursor: A already shown it
        self.set([("ccc111", "job-c", "blocked", ""), ("ddd111", "job-d", "failed", "")]); self.poll()
        b = self.hook("B")
        self.assertIn("failed (no launcher): job-d", b); self.assertNotIn("job-c", b)
        self.assertEqual(self.hook("A"), "")     # B delivered it; global cursor advanced

    def test_new_session_sees_undelivered_and_unresolved(self):
        self.poll()
        self.set([("eee111", "job-e", "failed", "")]); self.poll()
        s = self.hook("N1")
        self.assertIn("job-e", s); self.assertIn("resolve", s)
        self.assertEqual(self.hook("N1"), "")
        # delivered already; a later new session still gets the still-failed row once
        s2 = self.hook("N2")
        self.assertIn("UNRESOLVED failed (unstamped): job-e", s2)
        self.assertEqual(self.hook("N2"), "")
        self.set([]); self.poll()
        self.assertEqual(self.hook("N3"), "")  # resolved -> gone

    def sidecar(self, jid, cls, cwd, archive=False, mode="two-phase"):
        d = os.path.join(self.d, "logs", "archive") if archive else os.path.join(self.d, "logs")
        os.makedirs(d, exist_ok=True)
        spec = os.path.join(self.d, f"{jid}.spec.json")
        if mode:
            json.dump({"mode": mode, "test_file": "t.test.js"}, open(spec, "w"))
        json.dump({"id": jid, "status": "failed", "runner": "/x/bin/mini-swe-runner.py", "failure_class": cls, "cwd": cwd,
                   "task_file": spec, "runner_verdict": f"FAIL (mode={mode} phase-b RED test_green)"},
                  open(os.path.join(d, f"{jid}.done.json"), "w"))

    def test_failed_event_carries_finish_hint(self):
        work = os.path.join(self.d, "work"); os.makedirs(work)
        self.sidecar("fff111", "MODEL", work)
        self.sidecar("ggg111", "spec", work)
        self.sidecar("hhh111", "timeout", work, archive=True)  # an archived sidecar still counts
        self.sidecar("iii111", "model", os.path.join(self.d, "gone"))  # no work dir: nothing to finish in place
        self.sidecar("jjj111", "model", work, mode="verify")  # mini-finish refuses a verify-mode spec
        self.sidecar("kkk111", "model", work, mode=None)  # the spec is gone: mini-finish cannot read it
        self.poll()
        ids = ("fff111", "ggg111", "hhh111", "iii111", "jjj111", "kkk111")
        self.set([(i, f"job-{i[0]}", "failed", "") for i in ids]); self.poll()
        fin = {e["id"]: e.get("finish") for e in self.events()}
        self.assertEqual(fin, {"fff111": "~/bin/mini-finish fff111", "ggg111": None, "hhh111": "~/bin/mini-finish hhh111",
                               "iii111": None, "jjj111": None, "kkk111": None})
        s = self.hook("N1")
        self.assertIn("finish=`~/bin/mini-finish fff111`", s); self.assertIn("THIS session", s)
        self.assertNotIn("mini-finish ggg111", s)
        s2 = self.hook("N2")  # a later session: the UNRESOLVED line keeps the hint
        self.assertIn("UNRESOLVED failed (unstamped): job-f [fff111] finish=`~/bin/mini-finish fff111`", s2)

    def test_auto_job_finish_hint_needs_the_fix_stage(self):
        """An auto job gets finish= only when it reached its fix stage (the runner left spec.auto.json beside the work dir)."""
        run_fix, run_diag = os.path.join(self.d, "run-fix"), os.path.join(self.d, "run-diag")
        for r in (run_fix, run_diag):
            os.makedirs(os.path.join(r, "work"))
        json.dump({"mode": "two-phase", "test_file": "t.test.js"}, open(os.path.join(run_fix, "spec.auto.json"), "w"))
        self.sidecar("aaa222", "model", os.path.join(run_fix, "work"), mode="auto")
        self.sidecar("bbb222", "model", os.path.join(run_diag, "work") + "/", mode="auto")
        self.poll()
        self.set([(i, f"job-{i[0]}", "failed", "") for i in ("aaa222", "bbb222")]); self.poll()
        fin = {e["id"]: e.get("finish") for e in self.events()}
        self.assertEqual(fin, {"aaa222": "~/bin/mini-finish aaa222", "bbb222": None})


if __name__ == "__main__":
    unittest.main()
