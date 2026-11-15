#!/usr/bin/env python3
"""Tests for queue-agent.py. Run on claude-sandbox (never on the Mac):  python3 -m unittest test_queue_agent -v

Fixture files are written here in the FORMAT the Mac producers use, with invented content. Mac-only commands
(vm_stat, sysctl, ioreg, osascript, security) are faked or skipped. The HTTP tests run a server on loopback
inside the sandbox only.
"""
import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
_TMP = tempfile.mkdtemp(prefix="qagent-test-")
# Module-level constants in ollama-queue.py read these at import time.
os.environ["DARKBLOOM_PROVIDER_LOG"] = str(Path(_TMP) / "provider.log")
os.environ["DARKBLOOM_SLOTS_LOG"] = str(Path(_TMP) / "sandbox-slots.log")
os.environ["QUEUE_AGENT_CODE_DIR"] = str(HERE)
os.environ["INFRA_INCIDENTS_SCREEN_LOCKED"] = "0"

spec = importlib.util.spec_from_file_location("queue_agent", str(HERE / "queue-agent.py"))
qa = importlib.util.module_from_spec(spec)
sys.modules["queue_agent"] = qa
spec.loader.exec_module(qa)

PROV = Path(os.environ["DARKBLOOM_PROVIDER_LOG"])
SLOTS = Path(os.environ["DARKBLOOM_SLOTS_LOG"])
TOKEN = "test-token-not-real"


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hold_lines(transition="held"):
    return "\n".join([
        "Checking for provider update...",
        "Server started and listening on 127.0.0.1",
        json.dumps({"operation": "addon_hold", "transition": "begin", "lease_seconds": 60}),
        json.dumps({"operation": "addon_hold", "transition": transition, "lease_seconds": 60}),
    ]) + "\n"


class Fixture(unittest.TestCase):
    def setUp(self):
        for p in (PROV, SLOTS):
            if p.exists():
                p.unlink()
        qa.HOLD.since = None
        self.tmp = Path(tempfile.mkdtemp(prefix="qagent-case-"))


class AuthTests(unittest.TestCase):
    def test_bearer_ok_and_bad(self):
        self.assertTrue(qa.auth_ok(f"Bearer {TOKEN}", TOKEN))
        self.assertFalse(qa.auth_ok("Bearer nope", TOKEN))
        self.assertFalse(qa.auth_ok(None, TOKEN))
        self.assertFalse(qa.auth_ok(TOKEN, TOKEN))          # no scheme
        self.assertFalse(qa.auth_ok("Basic abc", TOKEN))

    def test_no_token_only_when_loopback(self):
        self.assertIsNone(qa.check_startup({"bind": "127.0.0.1"}, None))
        self.assertIsNone(qa.check_startup({}, None))
        self.assertIn("refusing", qa.check_startup({"bind": "203.0.113.148"}, None))
        self.assertIsNone(qa.check_startup({"bind": "203.0.113.148"}, "x"))

    def test_config_nested_and_flat(self):
        d = Path(tempfile.mkdtemp())
        (d / "a.toml").write_text('[queue_agent]\nbind = "203.0.113.193"\nport = 7000\n')
        self.assertEqual(qa.load_config(d / "a.toml"), {"bind": "203.0.113.193", "port": 7000})
        (d / "b.toml").write_text('"queue_agent.bind" = "203.0.113.2"\n')
        self.assertEqual(qa.load_config(d / "b.toml")["bind"], "203.0.113.2")
        self.assertEqual(qa.load_config(d / "missing.toml"), {})

    def test_token_file(self):
        d = Path(tempfile.mkdtemp())
        (d / "t").write_text(" abc \n")
        self.assertEqual(qa.read_token(d / "t"), "abc")
        self.assertIsNone(qa.read_token(d / "none"))


class HoldTests(Fixture):
    def test_held_with_fresh_log(self):
        PROV.write_text(hold_lines("renewed"))
        SLOTS.write_text(f"{iso(time.time())} slots total=2 jobs=1\n")
        out = qa.hold_payload()
        self.assertTrue(out["hold"])
        self.assertEqual(out["cause"], "sandbox-hold")
        self.assertEqual(out["view"]["sandbox_jobs"], 1)
        self.assertEqual(out["policy"], "fail-open")
        self.assertIsNotNone(out["view"]["hold_since"])
        self.assertTrue(out["inputs"]["provider_log"]["exists"])
        self.assertLess(out["inputs"]["provider_log"]["age_s"], 5)
        self.assertEqual(out["freshness_s"]["hold_stale"], 180.0)
        self.assertEqual(out["freshness_s"]["view_fresh"], 120.0)
        self.assertEqual(out["freshness_s"]["slots_fresh"], 300.0)

    def test_hold_since_is_kept_then_cleared(self):
        PROV.write_text(hold_lines())
        a = qa.hold_payload(now=time.time())["view"]["hold_since"]
        time.sleep(0.01)
        b = qa.hold_payload(now=time.time())["view"]["hold_since"]
        self.assertEqual(a, b)
        PROV.write_text(hold_lines() + json.dumps({"operation": "addon_hold", "transition": "released"}) + "\n")
        out = qa.hold_payload()
        self.assertFalse(out["hold"])
        self.assertIsNone(qa.HOLD.since)

    def test_stale_log_is_not_held(self):
        PROV.write_text(hold_lines())
        old = time.time() - 1000
        os.utime(PROV, (old, old))
        self.assertFalse(qa.hold_payload()["hold"])

    def test_missing_log_fails_open(self):
        out = qa.hold_payload()
        self.assertFalse(out["hold"])
        self.assertTrue(out["view"].get("unreadable"))
        self.assertFalse(out["inputs"]["provider_log"]["exists"])

    def test_stale_slots_line_is_none(self):
        PROV.write_text(hold_lines())
        SLOTS.write_text(f"{iso(time.time() - 1000)} slots total=2 jobs=1\n")
        self.assertIsNone(qa.hold_payload()["view"]["sandbox_jobs"])

    def test_hold_wait_note_when_model_given(self):
        PROV.write_text(hold_lines())
        out = qa.hold_payload(model="some-model")
        self.assertIsNotNone(out["hold_wait"])
        PROV.write_text("Server started and listening\n")
        self.assertIsNone(qa.hold_payload(model="some-model")["hold_wait"])

    def test_matches_queue_helper_exactly(self):
        PROV.write_text(hold_lines())
        q = qa.queue_mod()
        now = time.time()
        want = q.darkbloom_view(now)
        got = qa.hold_payload(now=now)["view"]
        for k in ("cause", "hold", "lease_s", "renewals", "log_age_s", "sandbox_jobs", "unreadable"):
            self.assertEqual(got.get(k), want.get(k), k)


class MacTests(unittest.TestCase):
    VM = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
          'Pages free:                               1000.\n'
          'Pages active:                             5000.\n'
          'Pages inactive:                           2000.\n'
          'Pages speculative:                         500.\n'
          'Pages purgeable:                           500.\n'
          'Pages wired down:                         1500.\n')

    def fake_run(self, cmd, timeout=3):
        return "1000000\n" if cmd[0] == "sysctl" else self.VM

    def test_memory_numbers(self):
        m = qa.memory_facts(self.fake_run)
        # total 1,000,000 bytes... use page math: reclaim = 4000 pages * 16384 = 65,536,000 > total, so used = 0
        self.assertEqual(m["page_size"], 16384)
        self.assertEqual(m["used_gb"], 0.0)
        self.assertEqual(m["pages"]["pages free"], 1000)

    def test_memory_used_arithmetic(self):
        total = 100_000_000
        (used, tot), pages = qa.parse_vm_stat(self.VM, 16384, total)
        self.assertEqual(tot, round(total / 1e9, 1))
        self.assertEqual(used, round((total - 4000 * 16384) / 1e9, 1))

    def test_memory_failure_is_none(self):
        def boom(cmd, timeout=3):
            raise FileNotFoundError("vm_stat")
        m = qa.memory_facts(boom)
        self.assertIsNone(m["used_gb"])

    def test_unparseable(self):
        (u, t), _ = qa.parse_vm_stat("garbage", 4096, 100)
        self.assertEqual((u, t), (None, None))

    def test_mac_payload_shape(self):
        out = qa.mac_payload(run=self.fake_run)
        self.assertIs(out["screen_locked"], False)           # INFRA_INCIDENTS_SCREEN_LOCKED=0
        self.assertIn("generated_at", out)
        self.assertEqual(len(out["load_avg"]), 3)


class LanePlanTests(Fixture):
    def make_sources(self):
        lp = qa.lane_plan_mod()
        d = self.tmp
        (d / "local.json").write_text(json.dumps({"base_url": "http://203.0.113.94:8000", "api_key": "SECRET-KEY-VALUE",
                                                  "pid": os.getpid(), "version": "1.2.3"}))
        (d / "daemon-state.json").write_text(json.dumps({"process_identity": {"pid": os.getpid(),
                                                                                  "start_time_micros": 5},
                                                           "advertised_models": ["m-one"]}))
        (d / "loaded-models.json").write_text(json.dumps({"models": ["m-one"]}))
        (d / "provider.pid").write_text(str(os.getpid()))
        (d / "provider.toml").write_text('enabled_models = ["m-two"]\n')
        (d / "hosts.json").write_text(json.dumps({"hosts": {"box": {"url": "http://198.51.100.226:11434/", "usable_bytes": 9}}}))
        (d / "bonsai-host").write_text("http://198.51.100.32:11434\n")
        return lp.Sources.under(d)

    def test_facts_and_no_key(self):
        out = qa.lane_plan_payload(sources=self.make_sources())
        text = json.dumps(out)
        self.assertNotIn("SECRET-KEY-VALUE", text)
        f = out["facts"]
        self.assertTrue(f["local_json"]["has_api_key"])
        self.assertEqual(f["local_json"]["base_url"], "http://203.0.113.94:8000")
        self.assertEqual(f["provider_pid"], {"pid": os.getpid(), "alive": True})
        self.assertEqual(f["provider_toml_served"], ["m-two"])
        self.assertEqual(f["hosts"]["box"]["url"], "http://198.51.100.226:11434")
        self.assertEqual(f["bonsai_url"], "http://198.51.100.32:11434")
        self.assertTrue(out["files"]["local_json"]["exists"])
        self.assertEqual(out["snapshot"]["darkbloom"]["state"], "up")

    def test_dead_pid_and_missing_files(self):
        src = self.make_sources()
        out = qa.lane_plan_payload(sources=src, pid_alive=lambda p: False)
        self.assertFalse(out["facts"]["provider_pid"]["alive"])
        (self.tmp / "local.json").unlink()
        out = qa.lane_plan_payload(sources=src)
        self.assertFalse(out["files"]["local_json"]["exists"])
        self.assertEqual(out["snapshot"]["darkbloom"]["state"], "off")


class NotifyTests(unittest.TestCase):
    def ok(self, **kw):
        b = {"title": "T", "body": "B"}
        b.update(kw)
        return qa.validate_notify(b)

    def test_valid(self):
        c, bad = self.ok(url="https://example.com/x", dedupe_key="stall:abc", dedupe_s=60)
        self.assertIsNone(bad)
        self.assertEqual(c["dedupe_s"], 60)

    def test_rejects(self):
        for kw in ({"title": ""}, {"title": "x" * 121}, {"body": "y" * 1001}, {"title": "a\nb"},
                   {"body": "a\x00b"}, {"url": "file:///etc/passwd"}, {"url": "javascript:1"},
                   {"dedupe_key": "a b; rm -rf /"}, {"dedupe_s": 0}, {"dedupe_s": "5"}, {"title": 5}):
            c, bad = self.ok(**kw)
            self.assertIsNone(c, kw)
            self.assertTrue(bad)
        self.assertIsNone(qa.validate_notify([1])[0])
        self.assertIsNone(qa.validate_notify({"body": "x"})[0])

    def test_body_newlines_allowed(self):
        self.assertIsNone(self.ok(body="line1\nline2")[1])

    def test_rate_limit(self):
        r = qa.RateLimiter(3, 100.0)
        self.assertTrue(all(r.take(now=t)[0] for t in (0, 1, 2)))
        ok, retry = r.take(now=3)
        self.assertFalse(ok)
        self.assertGreaterEqual(retry, 1)
        self.assertTrue(r.take(now=101)[0])          # the first hit aged out

    def test_do_notify_passes_args(self):
        seen = []
        c, _ = self.ok(dedupe_key="k")
        ch = qa.do_notify(c, lambda *a: seen.append(a) or "desktop")
        self.assertEqual(ch, "desktop")
        self.assertEqual(seen[0], ("T", "B", None, "k", 3600))

    def test_real_notify_dedupe_with_fake_sender(self):
        """notify-operator's own dedupe, driven through its sender hook (no keychain, no osascript)."""
        np = qa.notify_mod()
        d = Path(tempfile.mkdtemp())
        np.LOG = d / "log.jsonl"
        np._deduped.__defaults__ = (d / "dedupe.json",)      # the default path is bound at def time
        calls = []
        sender = lambda t, m, u: calls.append(t) or "desktop"
        self.assertEqual(np.notify("t", "m", dedupe_key="k", dedupe_s=60, now=1000.0, sender=sender), "desktop")
        self.assertEqual(np.notify("t", "m", dedupe_key="k", dedupe_s=60, now=1010.0, sender=sender), "deduped")
        self.assertEqual(len(calls), 1)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sent = []
        cls.srv, cls.ctx = qa.build_server(
            {"bind": "127.0.0.1", "port": 0, "notify_per_hour": 2}, TOKEN,
            notify=lambda *a: cls.sent.append(a) or "desktop",
            hold=lambda model=None: {"hold": False, "model": model, "generated_at": 1},
            mac=lambda: {"screen_locked": None, "generated_at": 1},
            lane=lambda: {"facts": {}, "generated_at": 1},
            dbstatus=lambda: {"text": "Provider: running\n", "generated_at": 1},
            ledger=lambda: {"mode": "on", "ledger": "ok", "rows": [], "generated_at": 1},
            dbconfigs=lambda: {"configs": {"models--org--m": {"num_hidden_layers": 4}}, "generated_at": 1})
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def req(self, method, path, body=None, token=TOKEN, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        if data:
            h["Content-Type"] = "application/json"
        c.request(method, path, data, h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, json.loads(raw), r

    def test_new_read_routes(self):
        st, body, _ = self.req("GET", "/v1/darkbloom-status")
        self.assertEqual((st, body["text"]), (200, "Provider: running\n"))
        st, body, _ = self.req("GET", "/v1/bloomgauge-ledger")
        self.assertEqual((st, body["mode"], body["rows"]), (200, "on", []))
        st, body, _ = self.req("GET", "/v1/darkbloom-model-configs")
        self.assertEqual((st, body["configs"]), (200, {"models--org--m": {"num_hidden_layers": 4}}))

    def test_model_configs_payload(self):
        root = Path(tempfile.mkdtemp(prefix="qagent-hf-"))
        def put(folder, snap, text):
            d = root / folder / "snapshots" / snap
            d.mkdir(parents=True, exist_ok=True)
            (d / "config.json").write_text(text)
        put("models--Org--Model-A", "a1", "not json")                 # first snapshot unreadable: the next one counts
        put("models--Org--Model-A", "b2", json.dumps({"num_hidden_layers": 40}))
        put("models--Org--Big", "s", json.dumps({"pad": "x" * (qa.MODEL_CONFIG_MAX_BYTES + 1)}))   # too big: skipped
        put("datasets--x", "s", json.dumps({"a": 1}))                   # not a model folder
        put("models--Org--List", "s", json.dumps([1, 2]))               # not an object
        (root / "models--Org--NoSnap").mkdir()
        (root / "stray.txt").write_text("x")
        out = qa.darkbloom_model_configs_payload(now=5, root=root)
        self.assertEqual(out, {"generated_at": 5, "configs": {"models--Org--Model-A": {"num_hidden_layers": 40}}})
        self.assertEqual(qa.darkbloom_model_configs_payload(now=5, root=root / "missing")["configs"], {})
        # unset cache directory: empty map, no error
        class FakeQ:
            @staticmethod
            def _darkbloom_cache_dir():
                return None
        saved = qa.queue_mod
        qa.queue_mod = lambda: FakeQ
        try:
            self.assertEqual(qa.darkbloom_model_configs_payload(now=5)["configs"], {})
        finally:
            qa.queue_mod = saved

    def test_status_cli_timeout_under_client_wait(self):
        # the container's client waits 5 s; the agent's CLI run must give up before that
        self.assertLess(qa.DARKBLOOM_STATUS_CLI_TIMEOUT_S, 5)
        seen = {}
        class FakeQ:
            @staticmethod
            def _darkbloom_status_cli(timeout=8):
                seen["timeout"] = timeout
                return "Provider: running\n"
        saved = qa.queue_mod
        qa.queue_mod = lambda: FakeQ
        try:
            qa._status_cache.update(at=0.0, text="")
            out = qa.darkbloom_status_payload(now=10_000.0)
        finally:
            qa.queue_mod = saved
            qa._status_cache.update(at=0.0, text="")
        self.assertEqual((out["text"], seen["timeout"]), ("Provider: running\n", qa.DARKBLOOM_STATUS_CLI_TIMEOUT_S))

    def test_ledger_payload_tail_and_states(self):
        d = Path(tempfile.mkdtemp(prefix="qagent-led-"))
        led = d / "l.jsonl"
        led.write_text("".join(json.dumps({"ts": i}) + "\n" for i in range(50)))
        out = qa.bloomgauge_ledger_payload(now=1, cfg={"mode": "on", "ledger_path": str(led)}, tail_bytes=100)
        self.assertEqual(out["ledger"], "ok")
        self.assertTrue(all(json.loads(r)["ts"] is not None for r in out["rows"]))   # no cut first line
        self.assertEqual(json.loads(out["rows"][-1])["ts"], 49)
        out = qa.bloomgauge_ledger_payload(now=1, cfg={"mode": "off", "ledger_path": str(d / "none")})
        self.assertEqual((out["ledger"], out["mode"], out["rows"]), ("missing", "off", []))

    def test_health_is_open(self):
        st, body, _ = self.req("GET", "/health", token=None)
        self.assertEqual(st, 200)
        self.assertTrue(body["ok"])
        self.assertIn("generated_at", body)

    def test_auth_required(self):
        for p in ("/v1/hold", "/v1/mac", "/v1/lane-plan", "/v1/darkbloom-status", "/v1/bloomgauge-ledger",
                  "/v1/darkbloom-model-configs"):
            st, body, _ = self.req("GET", p, token=None)
            self.assertEqual((st, body["status"]), (401, 401), p)
            st, _, _ = self.req("GET", p, token="wrong")
            self.assertEqual(st, 401)
        st, _, _ = self.req("POST", "/v1/notify", {"title": "a", "body": "b"}, token=None)
        self.assertEqual(st, 401)

    def test_get_routes(self):
        st, body, _ = self.req("GET", "/v1/hold?model=abc")
        self.assertEqual((st, body["model"]), (200, "abc"))
        self.assertEqual(self.req("GET", "/v1/mac")[0], 200)
        self.assertEqual(self.req("GET", "/v1/lane-plan")[0], 200)
        st, body, _ = self.req("GET", "/v1/nope")
        self.assertEqual((st, body["error"]), (404, "not found"))

    def test_notify_flow_and_rate_limit(self):
        self.sent.clear()
        self.ctx.limiter.hits.clear()
        st, body, _ = self.req("POST", "/v1/notify", {"title": "a", "body": "b"})
        self.assertEqual((st, body["channel"]), (200, "desktop"))
        st, body, _ = self.req("POST", "/v1/notify", {"title": "", "body": "b"})
        self.assertEqual(st, 400)
        self.assertEqual(self.req("POST", "/v1/notify", {"title": "c", "body": "d"})[0], 200)
        st, body, r = self.req("POST", "/v1/notify", {"title": "e", "body": "f"})
        self.assertEqual(st, 429)
        self.assertTrue(r.getheader("Retry-After"))
        self.assertEqual(len(self.sent), 2)          # invalid and limited requests never reached notify

    def test_notify_bad_body(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/v1/notify", b"not json", {"Authorization": f"Bearer {TOKEN}", "Content-Length": "8"})
        r = c.getresponse()
        self.assertEqual(r.status, 400)
        r.read()
        c.close()

    def test_error_has_no_traceback(self):
        srv, _ = qa.build_server({"bind": "127.0.0.1", "port": 0}, TOKEN,
                                 hold=lambda model=None: 1 / 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            c.request("GET", "/v1/hold", headers={"Authorization": f"Bearer {TOKEN}"})
            r = c.getresponse()
            body = json.loads(r.read())
            self.assertEqual(r.status, 503)
            self.assertNotIn("Traceback", json.dumps(body))
            self.assertNotIn("division", json.dumps(body))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_responses_never_hold_token(self):
        _, body, _ = self.req("GET", "/v1/hold")
        self.assertNotIn(TOKEN, json.dumps(body))


class MirrorTests(Fixture):
    def ev(self, n):
        return {"seq": n, "ts": "2026-11-12T10:00:00+0000", "id": f"job{n}", "label": "x", "state": "done"}

    def test_events_append_and_dedupe(self):
        d = self.tmp / "ev"
        self.assertEqual(qa.apply_events(d, [self.ev(1), self.ev(2)]), 2)
        self.assertEqual(qa.apply_events(d, [self.ev(2), self.ev(3)]), 1)       # 2 already there
        lines = (d / "queue-events.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(x)["seq"] for x in lines], [1, 2, 3])
        self.assertEqual(qa.last_local_seq(d), 3)

    def test_events_half_line_and_rotation(self):
        d = self.tmp / "ev"
        d.mkdir()
        (d / "queue-events.jsonl").write_text(json.dumps(self.ev(1)) + "\n" + '{"seq": 2, "id"')
        qa.apply_events(d, [self.ev(5)])
        self.assertIn("\n" + json.dumps(self.ev(5)), (d / "queue-events.jsonl").read_text())
        qa.apply_events(d, [self.ev(6)], max_bytes=10)
        self.assertTrue((d / "queue-events.jsonl.1").exists())
        self.assertEqual(qa.last_local_seq(d), 6)

    def test_escalations_only_known_files(self):
        d = self.tmp / "esc"
        wrote = qa.apply_escalations(d, {"ESCALATIONS.md": "- [ ] one\n", "READY-TO-LAND.md": "- [ ] r\n",
                                         "../evil": "x", "other.md": "y"})
        self.assertEqual(sorted(wrote), ["ESCALATIONS.md", "READY-TO-LAND.md"])
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["ESCALATIONS.md", "READY-TO-LAND.md"])
        self.assertEqual(qa.apply_escalations(d, {"ESCALATIONS.md": "- [ ] one\n"}), [])     # unchanged
        self.assertEqual(qa.apply_escalations(d, {"ESCALATIONS.md": None}), [])              # None leaves it alone
        self.assertEqual((d / "ESCALATIONS.md").read_text(), "- [ ] one\n")

    def test_pull_once_with_fake_fetch(self):
        urls = []

        def fetch(url, token):
            urls.append((url, token))
            if "/api/mirror/events" in url:
                return {"events": [self.ev(1), self.ev(2)], "last_seq": 2,
                        "failed": {"ts": 1.0, "failed": [{"id": "j1"}]}}
            return {"files": {"ESCALATIONS.md": "- [ ] stuck\n", "READY-TO-LAND.md": None}}

        m = qa.Mirror("http://container.invalid:7684/", "tok", fetch=fetch, edir=self.tmp / "ev")
        self.assertEqual(m.once(), 2)
        self.assertEqual(urls[0][0], "http://container.invalid:7684/api/mirror/events?after=0&limit=200")
        self.assertEqual(urls[0][1], "tok")
        self.assertEqual(m.once(), 0)                                       # second pull: after=2, nothing new
        self.assertIn("after=2", urls[2][0])
        self.assertTrue((self.tmp / "ev" / "queue-events.failed.json").exists())
        self.assertEqual((self.tmp / "ev" / "escalations" / "ESCALATIONS.md").read_text(), "- [ ] stuck\n")
        self.assertIsNone(m.status["last_error"])

    def test_off_by_default(self):
        srv, ctx = qa.build_server({"bind": "127.0.0.1", "port": 0}, None)
        srv.server_close()
        self.assertIsNone(ctx.mirror)


class FakeBgt:
    """bloomgauge_tick's surface the agent uses: wanted(cfg, ctl), run_async(work, now, cfg), _STATE."""

    def __init__(self, wanted=True, started=True):
        self.want, self.started, self.calls = wanted, started, []
        self._STATE = {"thread": None, "last": 0.0, "alert": None}

    def wanted(self, cfg=None, ctl=None):
        return self.want

    def run_async(self, work, now=None, cfg=None, fn=None):
        self.calls.append((work, now, cfg, self._STATE.get("deliver")))
        if self.started:
            self._STATE["last"] = now
        return self.started


class FakeCtl:
    def __init__(self, mode="on"):
        self.mode = mode

    def config(self):
        return {"mode": self.mode, "tick_s": 30, "lease_path": "/nonexistent/lease.json"}


class BloomTickTests(unittest.TestCase):
    W = {"busy": True, "why": "1 lane row(s)", "target": "qwen3.6-35b"}

    def make(self, mode="on", wanted=True):
        self.sent = []
        self.bgt = FakeBgt(wanted=wanted)
        return qa.BloomTick(bgt=self.bgt, ctl=FakeCtl(mode), notify=lambda *a: self.sent.append(a) or "desktop",
                            clock=lambda: 500.0)

    def test_validate(self):
        ok = qa.validate_bg_work(dict(self.W, ack=3, epoch="e"))
        self.assertEqual(ok, (self.W, 3, "e", None))
        work, _, _, bad = qa.validate_bg_work({"busy": False, "why": "a\x00b\nc " + "x" * 400, "target": None})
        self.assertIsNone(bad)
        self.assertTrue(work["why"].startswith("a b c ") and len(work["why"]) == qa.BG_WHY_MAX)
        for body in ([], {"busy": "yes"}, {"busy": True, "why": 5}, {"busy": True, "target": 7},
                     {"busy": True, "target": "m; rm -rf /"}, {"busy": True, "target": ""},
                     {"busy": True, "ack": -1}, {"busy": True, "ack": True}, {"busy": True, "epoch": "e" * 65}):
            self.assertIsNotNone(qa.validate_bg_work(body)[3], body)

    def test_not_wanted_runs_nothing(self):
        bt = self.make(mode="off", wanted=False)
        ans, bad = bt.handle(dict(self.W))
        self.assertIsNone(bad)
        self.assertEqual((ans["mode"], ans["wanted"], ans["started"], ans["alerts"]), ("off", False, False, []))
        self.assertEqual(self.bgt.calls, [])

    def test_wanted_runs_the_step_here_without_an_owner_pid(self):
        bt = self.make()
        ans, bad = bt.handle(dict(self.W, ack=0, epoch=None))
        self.assertIsNone(bad)
        self.assertEqual((ans["mode"], ans["wanted"], ans["started"], ans["epoch"]), ("on", True, True, bt.epoch))
        work, now, cfg, deliver = self.bgt.calls[0]
        self.assertEqual((work, now), (self.W, 500.0))
        self.assertIn("owner_pid", cfg)
        self.assertIsNone(cfg["owner_pid"])
        self.assertEqual(cfg["mode"], "on")
        self.assertEqual(deliver, bt.deliver)
        self.assertEqual(ans["last_step_at"], 500.0)

    def test_alerts_kept_until_acked_in_the_same_epoch_and_notified_once(self):
        bt = self.make()
        bt.deliver("bloomgauge-control-pick", "3 picks\nfailed")
        self.bgt._STATE["alert"] = {"key": "bloomgauge-control-manager", "msg": "left for the queue"}
        ans, _ = bt.handle(dict(self.W))
        self.assertEqual([(a["seq"], a["key"], a["msg"]) for a in ans["alerts"]],
                         [(1, "bloomgauge-control-pick", "3 picks failed"),
                          (2, "bloomgauge-control-manager", "left for the queue")])
        self.assertIsNone(self.bgt._STATE.get("alert"))
        self.assertEqual(len(self.sent), 2)
        title, body, url, key, secs = self.sent[0]
        self.assertEqual((title, key, secs), ("BloomGauge control", "bloomgauge-control-pick", 3600))
        ans, _ = bt.handle(dict(self.W, ack=1, epoch="other-epoch"))       # not this service's numbers: kept
        self.assertEqual(len(ans["alerts"]), 2)
        ans, _ = bt.handle(dict(self.W, ack=1, epoch=bt.epoch))
        self.assertEqual([a["seq"] for a in ans["alerts"]], [2])
        ans, _ = bt.handle(dict(self.W, ack=2, epoch=bt.epoch))
        self.assertEqual(ans["alerts"], [])
        self.assertEqual(len(self.sent), 2)

    def test_alert_list_is_capped_and_keys_are_plain(self):
        bt = self.make()
        for i in range(qa.BG_ALERTS_MAX + 5):
            bt.deliver("bad key!$", f"m{i}")
        al = bt.status()["alerts"]
        self.assertEqual(len(al), qa.BG_ALERTS_MAX)
        self.assertEqual((al[0]["seq"], al[-1]["seq"]), (6, qa.BG_ALERTS_MAX + 5))
        self.assertEqual(al[0]["key"], "bad-key--")

    def test_notify_failure_keeps_the_alert(self):
        bt = qa.BloomTick(bgt=FakeBgt(), ctl=FakeCtl(), notify=lambda *a: (_ for _ in ()).throw(OSError("no")))
        bt.deliver("bloomgauge-control-log", "log unreadable")
        self.assertEqual(len(bt.status()["alerts"]), 1)

    def test_real_tick_module_loads_and_answers(self):
        # the real bloomgauge_tick/bloomgauge_control from this tree, mode off and no lease: nothing runs
        home = tempfile.mkdtemp(prefix="qagent-bg-")
        saved = {k: os.environ.get(k) for k in ("QUEUE_BG_HOME", "QUEUE_BLOOMGAUGE_CONTROL")}
        os.environ["QUEUE_BG_HOME"], os.environ["QUEUE_BLOOMGAUGE_CONTROL"] = home, "off"
        try:
            bt = qa.BloomTick(notify=lambda *a: "desktop")
            ans, bad = bt.handle(dict(self.W))
            self.assertIsNone(bad)
            self.assertEqual((ans["mode"], ans["wanted"], ans["started"]), ("off", False, False))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class BloomTickHttpTests(unittest.TestCase):
    def setUp(self):
        self.bgt = FakeBgt()
        self.bt = qa.BloomTick(bgt=self.bgt, ctl=FakeCtl(), notify=lambda *a: "desktop", clock=lambda: 7.0)
        self.srv, _ = qa.build_server({"bind": "127.0.0.1", "port": 0}, TOKEN, bgtick=self.bt)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def req(self, method, path, body=None, token=TOKEN, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        h = {"Authorization": f"Bearer {token}"} if token else {}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            h["Content-Type"] = "application/json"
        c.request(method, path, data, h)
        r = c.getresponse()
        out = (r.status, json.loads(r.read()))
        c.close()
        return out

    def test_post_and_get(self):
        st, ans = self.req("POST", "/v1/bloomgauge-tick", {"busy": False, "why": "queue done", "target": None})
        self.assertEqual((st, ans["started"], ans["mode"]), (200, True, "on"))
        self.assertEqual(self.bgt.calls[0][1], 7.0)
        st, ans = self.req("GET", "/v1/bloomgauge-tick")
        self.assertEqual((st, ans["mode"], ans["started"], ans["last_step_at"]), (200, "on", None, 7.0))

    def test_auth_and_bad_bodies(self):
        self.assertEqual(self.req("POST", "/v1/bloomgauge-tick", {"busy": True}, token=None)[0], 401)
        self.assertEqual(self.req("POST", "/v1/bloomgauge-tick", {"busy": True}, token="wrong")[0], 401)
        self.assertEqual(self.req("GET", "/v1/bloomgauge-tick", token=None)[0], 401)
        self.assertEqual(self.req("POST", "/v1/bloomgauge-tick", {"busy": "x"})[0], 400)
        self.assertEqual(self.req("POST", "/v1/bloomgauge-tick", raw=b"{not json")[0], 400)
        self.assertEqual(self.req("POST", "/v1/bloomgauge-tick", raw=b"x" * (qa.MAX_BODY + 1))[0], 413)
        self.assertEqual(self.req("POST", "/v1/other", {"busy": True})[0], 404)
        self.assertEqual(self.bgt.calls, [])

    def test_a_failing_step_is_503(self):
        def boom(*a, **k):
            raise RuntimeError("bloomgauge module broke")
        self.bgt.run_async = boom
        st, ans = self.req("POST", "/v1/bloomgauge-tick", {"busy": True, "target": None})
        self.assertEqual((st, ans["error"]), (503, "control step failed"))


if __name__ == "__main__":
    unittest.main()
