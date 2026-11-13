#!/usr/bin/env python3
"""Container side of the Mac status service: the queue and the janitor read the vendor facts over QUEUE_AGENT_URL.
Run on claude-sandbox (never on the Mac):  python3 -m unittest test_queue_agent_client -v
A fake agent runs on loopback inside the sandbox. Unset QUEUE_AGENT_URL must give today's file paths."""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
_TMP = tempfile.mkdtemp(prefix="qac-test-")
os.environ.setdefault("DARKBLOOM_PROVIDER_LOG", str(Path(_TMP) / "provider.log"))
os.environ.setdefault("DARKBLOOM_SLOTS_LOG", str(Path(_TMP) / "sandbox-slots.log"))
sys.path.insert(0, str(HERE))
import queue_config as qc


def _load(name, fn):
    spec = importlib.util.spec_from_file_location(name, str(HERE / fn))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


Q = _load("oq_client_test", "ollama-queue.py")
J = _load("eij_client_test", "escalation_index_janitor.py")
TOKEN = "agent-token-not-real"
ANSWERS = {}
SEEN = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        SEEN.append((self.path, self.headers.get("Authorization")))
        a = ANSWERS.get(self.path)
        if a is None:
            self.send_response(503); self.end_headers(); return
        raw = json.dumps(a).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), H)
        cls.url = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def setUp(self):
        ANSWERS.clear(); SEEN.clear()
        self.saved = dict(os.environ)
        os.environ["QUEUE_AGENT_URL"] = self.url
        os.environ["QUEUE_AGENT_TOKEN"] = TOKEN
        Q._AGENT_LANE_FACTS.update(facts=None, at=0.0, tried=None)

    def tearDown(self):
        os.environ.clear(); os.environ.update(self.saved)


class QueueSide(Base):
    def test_status_cli_via_agent(self):
        ANSWERS["/v1/darkbloom-status"] = {"text": "Provider: running\n"}
        self.assertEqual(Q._darkbloom_status_cli(), "Provider: running\n")
        self.assertEqual(SEEN[0], ("/v1/darkbloom-status", "Bearer " + TOKEN))

    def test_status_cli_agent_down_is_empty(self):
        self.assertEqual(Q._darkbloom_status_cli(), "")

    def test_served_exact_from_lane_facts(self):
        ANSWERS["/v1/lane-plan"] = {"facts": {"provider_toml_served": ["Org/Model-A"],
                                              "loaded_models": {"models": ["Org/Model-B", "Org/Model-A"]}}}
        self.assertEqual(Q._agent_served_exact(), ["Org/Model-A", "Org/Model-B"])

    def test_served_exact_agent_down(self):
        self.assertEqual(Q._agent_served_exact(), [])

    def test_agent_down_is_asked_once_per_ttl(self):
        # a silent Mac must not cost a 3 s timeout per lookup: several lookups in one poll = one ask
        for _ in range(4):
            self.assertEqual(Q._agent_served_exact(), [])
        self.assertEqual([p for p, _ in SEEN], ["/v1/lane-plan"])
        Q._AGENT_LANE_FACTS["tried"] -= Q.AGENT_FACTS_TTL_S        # TTL passed: asked again
        ANSWERS["/v1/lane-plan"] = {"facts": {"provider_toml_served": ["Org/M"]}}
        self.assertEqual(Q._agent_served_exact(), ["Org/M"])
        self.assertEqual(len(SEEN), 2)

    def test_hold_view_from_agent_resets_clock(self):
        ANSWERS["/v1/hold"] = {"view": {"cause": "addon hold", "hold": True, "unreadable": False, "at": 1.0}}
        v = Q.darkbloom_view(now=500.0)
        self.assertTrue(v["hold"])
        self.assertEqual(v["at"], 500.0)

    def test_hold_view_agent_down_is_fail_open(self):
        v = Q.darkbloom_view(now=500.0)
        self.assertFalse(v["hold"])
        self.assertTrue(v["unreadable"])

    def test_agent_unset_reads_files_not_network(self):
        del os.environ["QUEUE_AGENT_URL"]
        ANSWERS["/v1/hold"] = {"view": {"hold": True}}
        v = Q.darkbloom_view(now=500.0)           # provider.log absent in the sandbox tmp
        self.assertTrue(v["unreadable"])
        self.assertEqual(SEEN, [])


class JanitorSide(Base):
    ROW = json.dumps({"ts": 100.0, "phase": "result", "action": "manager_on", "mode": "on"})

    def test_mode_and_rows_via_agent(self):
        ANSWERS["/v1/bloomgauge-ledger"] = {"mode": "on", "ledger": "ok", "rows": [self.ROW]}
        f = J.Facts(runs_dir=_TMP, plans_dir=_TMP, queue_state=Path(_TMP) / "none.json")
        self.assertEqual(f.bg_control_mode(), "on")
        self.assertEqual(f.bg_last_write({"manager_on"}).get("ts"), 100.0)
        self.assertEqual(sum(1 for p, _ in SEEN if p == "/v1/bloomgauge-ledger"), 1)   # one fetch per run

    def test_agent_down_is_unreadable(self):
        f = J.Facts(runs_dir=_TMP, plans_dir=_TMP, queue_state=Path(_TMP) / "none.json")
        self.assertIsNone(f.bg_control_mode())
        self.assertIsNone(f.bg_last_write({"manager_on"}))

    def test_ledger_unreadable_on_mac(self):
        ANSWERS["/v1/bloomgauge-ledger"] = {"mode": "on", "ledger": "unreadable", "rows": []}
        f = J.Facts(runs_dir=_TMP, plans_dir=_TMP, queue_state=Path(_TMP) / "none.json")
        self.assertIsNone(f.bg_last_write({"manager_on"}))

    def test_agent_unset_uses_config(self):
        del os.environ["QUEUE_AGENT_URL"]
        led = Path(_TMP) / "led.jsonl"
        led.write_text(self.ROW + "\n")
        f = J.Facts(runs_dir=_TMP, plans_dir=_TMP, queue_state=Path(_TMP) / "none.json",
                    bg_config={"mode": "on", "ledger_path": str(led)})
        self.assertEqual(f.bg_control_mode(), "on")
        self.assertEqual(f.bg_last_write({"manager_on"}).get("ts"), 100.0)
        self.assertEqual(SEEN, [])


if __name__ == "__main__":
    unittest.main()
