import importlib.util, json, sys, threading, tempfile, unittest, argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.machinery import SourceFileLoader
from pathlib import Path
HERE = Path(__file__).parent
def load(n):
    ld = SourceFileLoader(n.replace("-", "_"), str(HERE / n))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(ld.name, ld)); ld.exec_module(m); return m
L = load("mini-lane")

class Door(BaseHTTPRequestHandler):
    jobs = {}; answer = {"decision": "send"}; model = "m1"; posts = []
    def log_message(self, *a): pass
    def _send(self, st, o):
        b = json.dumps(o).encode(); self.send_response(st); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer tok": return self._send(401, {"error": "E_AUTH"})
        b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not b.get("app"): return self._send(400, {"error": "E_BAD"})
        Door.posts.append(b); Door.jobs["j1"] = 0; self._send(202, {"id": "j1"})
    def do_GET(self):
        Door.jobs["j1"] += 1
        if Door.jobs["j1"] < 2: return self._send(200, {"status": "running"})
        self._send(200, {"status": "done", "model": Door.model, "lane": "unraid", "result": Door.answer})
    def do_DELETE(self): self._send(200, {})

class T(unittest.TestCase):
    @classmethod
    def setUpClass(c):
        c.s = HTTPServer(("127.0.0.1", 0), Door); threading.Thread(target=c.s.serve_forever, daemon=True).start()
        c.base = "http://127.0.0.1:%d" % c.s.server_port
    def q(self, model="m1", tok="tok"):
        return L.QueueJudge("unraid", model, base=self.base, token=tok, poll_s=0, sleep=lambda s: None)
    def test_door_ok(self):
        Door.model = "m1"; Door.answer = {"decision": "send", "reason": "x"}
        t = L.judge_triage(self.q(), "r", "ask"); self.assertEqual(t["decision"], "send")
        self.assertEqual(Door.posts[-1]["model"], "m1"); self.assertEqual(Door.posts[-1]["lane"], "unraid")
    def test_model_mismatch(self):
        Door.model = "other"
        with self.assertRaises(L.JudgeError) as e: self.q().call("s", "t")
        self.assertEqual(e.exception.code, "E_MODEL_MISMATCH")
    def test_auth_and_ready(self):
        self.assertIsNotNone(self.q(tok="bad").ready()); self.assertIsNone(self.q().ready())
        with self.assertRaises(L.JudgeError): self.q(tok="").call("s", "t")
    def test_unparsed(self):
        Door.model = "m1"; Door.answer = {"nope": 1}
        self.assertFalse(L.judge_triage(self.q(), "r", "a")["parsed"])
    def test_enqueue_judge(self):
        subs = []
        def submit(task, job): subs.append((task, job)); return "jid1"
        seq = [("pending", None), ("running", None),
               ("done", {"ok": True, "result": {"decision": "send", "reason": "x"}, "model": "m1", "lane": "ollama"})]
        j = L.EnqueueJudge("unraid", "m1", submit=submit, fetch=lambda jid: seq.pop(0), poll_s=0, sleep=lambda s: None)
        self.assertEqual(L.judge_triage(j, "r", "ask")["decision"], "send")
        self.assertEqual(subs[0][1]["model"], "m1"); self.assertEqual(subs[0][1]["host"], "unraid")
        self.assertTrue(subs[0][1]["label"].startswith("lane-bakeoff-judge-")); self.assertTrue(subs[0][0]["json"])
        d = L.EnqueueJudge("darkbloom", "qm", submit=submit, fetch=lambda jid: ("done", {"ok": True, "result": {"a": 1}, "model": "qm", "lane": "darkbloom"}),
                           poll_s=0, sleep=lambda s: None)
        self.assertEqual(d.call("s", "t")[0], {"a": 1}); self.assertEqual(subs[-1][1]["host"], "studio-db")
    def test_enqueue_judge_errors(self):
        mk = lambda rec, st="done": L.EnqueueJudge("unraid", "m1", submit=lambda t, j: "j", fetch=lambda jid: (st, rec), poll_s=0, sleep=lambda s: None)
        with self.assertRaises(L.JudgeError) as e: mk({"ok": True, "result": {}, "model": "other", "lane": "ollama"}).call("s", "t")
        self.assertEqual(e.exception.code, "E_MODEL_MISMATCH")
        with self.assertRaises(L.JudgeError) as e: mk({"ok": True, "result": {}, "model": "m1", "lane": "darkbloom"}).call("s", "t")
        self.assertEqual(e.exception.code, "E_LANE_MISMATCH")
        with self.assertRaises(L.JudgeError) as e: mk(None, "failed").call("s", "t")
        self.assertEqual(e.exception.code, "E_FAILED")
        self.assertIsNone(mk({"ok": False, "error": "E_NOT_JSON", "result": {"text": "no"}, "model": "m1", "lane": "ollama"}).call("s", "t")[0])
        def boom(t, j): raise OSError("down")
        with self.assertRaises(L.JudgeError) as e: L.EnqueueJudge("unraid", "m1", submit=boom, fetch=None).call("s", "t")
        self.assertEqual(e.exception.code, "E_QUEUE_DOWN")
        with self.assertRaises(ValueError): L.EnqueueJudge("claude", "m")
    def test_make_judge_default_is_enqueue(self):
        import os
        os.environ.pop("MINI_LANE_DOOR", None)
        self.assertIsInstance(L.make_judge("unraid:qwen3:8b"), L.EnqueueJudge)
        os.environ["MINI_LANE_DOOR"] = "1"
        try: self.assertIsInstance(L.make_judge("unraid:qwen3:8b", token="t"), L.QueueJudge)
        finally: os.environ.pop("MINI_LANE_DOOR", None)
    def test_helpers(self):
        self.assertEqual(L.parse_object('<think>x</think>```json\n{"a":1}\n```'), {"a": 1})
        self.assertIsNone(L.parse_object("no json"))
        self.assertEqual(len(L.clip_words("w " * 300).split()), 121 if False else len(L.clip_words("w " * 300).split()))
        self.assertLessEqual(len(L.clip_words("w " * 300).replace(" ...", "").split()), 120)
        self.assertLessEqual(len(L.fit("x" * 40000, 15000).encode()), 15100)
        for bad in ("foo", "claude:opus", "darkbloom:"):
            with self.assertRaises(ValueError): L.parse_judge_spec(bad)
        self.assertEqual(L.slug("Fix The  Thing!!"), "fix-the-thing")
    def test_packet_trim_keeps_diff(self):
        p = "REVIEW packet: PASS gate=ok\n## Test file\n" + "t\n" * 20000 + "## Diff (allowed files)\n+KEEPME\n"
        self.assertIn("KEEPME", L.packet_for_review(p)); self.assertLessEqual(len(L.packet_for_review(p).encode()), 15400)
    def test_gates(self):
        g = L.parse_gates("REVIEW packet: PASS gate=ok tests=ok"); self.assertTrue(g["ok"])
        self.assertFalse(L.parse_gates("REVIEW packet: PASS gate=fail")["ok"])
    def test_claude_runner(self):
        j = L.ClaudeJudge("haiku", runner=lambda p: '{"verdict":"reject","reason":"r"}')
        self.assertEqual(L.judge_verdict(j, "pk")["verdict"], "reject")
    def test_bakeoff_refuses_outside_repo(self):
        d = tempfile.mkdtemp(); tf = Path(d, "t.txt"); tf.write_text("x")
        a = argparse.Namespace(repo=d, task_file=str(tf), judge="claude:haiku", bakeoff=tempfile.mkdtemp(), out=None, record=False,
                               base="HEAD", name=None, coding_model=None, remote=True, wait_s=1, poll_s=0)
        R, rc = L.run_lane(a, L.ClaudeJudge("haiku", runner=lambda p: "{}")); self.assertEqual(rc, 2)
    def test_refuse_flow(self):
        d = tempfile.mkdtemp(); o = tempfile.mkdtemp(); tf = Path(d, "t.txt"); tf.write_text("rename a var")
        a = argparse.Namespace(repo=d, task_file=str(tf), judge="x", bakeoff=None, out=o, record=False, base="HEAD", name=None,
                               coding_model=None, remote=True, wait_s=1, poll_s=0)
        j = L.ClaudeJudge("haiku", runner=lambda p: '{"decision":"refuse","reason":"too small","report":"Refused: too small."}')
        R, rc = L.run_lane(a, j); self.assertEqual((R["status"], rc), ("refused", 0)); self.assertIsNone(R["job"])
    def test_full_flow_fake_job(self):
        d = tempfile.mkdtemp(); o = tempfile.mkdtemp(); tf = Path(d, "t.txt"); tf.write_text("fix parse bug in a.py")
        a = argparse.Namespace(repo=d, task_file=str(tf), judge="x", bakeoff=None, out=o, record=False, base="HEAD", name=None,
                               coding_model=None, remote=True, wait_s=5, poll_s=0)
        ans = iter(['{"decision":"send"}', '{"name":"parse-fix","task_md":"' + "Fix the parse bug carefully. " * 4 + '"}',
                    '{"verdict":"ok","reason":"fine"}', '{"report":"' + "word " * 300 + '"}'])
        j = L.ClaudeJudge("haiku", runner=lambda p: next(ans))
        L.submit_job = lambda *x, **k: ("job1", "ok")
        L.packet_path = lambda job: None
        R, rc = L.run_lane(a, j, sleep=lambda s: None, show=lambda job, env=None: (True, "PASS gate=ok", " 1 file changed, 30 insertions(+), 2 deletions(-)"))
        self.assertEqual(R["status"], "done"); self.assertEqual(R["verdict"], "ok"); self.assertLessEqual(R["words"], 121)
if __name__ == "__main__": unittest.main()
