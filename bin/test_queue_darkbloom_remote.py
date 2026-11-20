#!/usr/bin/env python3
"""The queue container finds Darkbloom's CURRENT key (and the relay address) from the Mac status service.
Run on claude-sandbox (never on the Mac):  python3 -m unittest test_queue_darkbloom_remote -v
A fake agent runs on loopback inside the sandbox; every address and key below is synthetic."""
import contextlib
import importlib.util
import io
import json
import logging
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
_TMP = tempfile.mkdtemp(prefix="qdb-remote-")
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


Q = _load("oq_dbremote_test", "ollama-queue.py")
AGENT_TOKEN = "agent-token-not-real"
AGENT_KEY = "agent-served-key-not-real"
ENV_KEY = "stale-env-key-not-real"
ENV_BASE = "http://192.0.2.20:8000"
RELAY_BASE = "http://192.0.2.21:8000"
UNRAID = "http://192.0.2.30:11434"
DB_MODEL = "qwen3.6-35b-a3b-vl-mtp-mxfp8"
ANSWER = {}
SEEN = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        SEEN.append((self.path, self.headers.get("Authorization")))
        if self.path != "/v1/darkbloom-endpoint" or "doc" not in ANSWER:
            self.send_response(503); self.end_headers(); return
        raw = json.dumps(ANSWER["doc"]).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class FakeW:
    KNOWN_OLLAMA_HOSTS = {"unraid": UNRAID}
    BIG_HOST_NAME = "studio"
    SMALL_HOST_NAME = "unraid"


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
        ANSWER.clear(); SEEN.clear()
        self.saved_env = dict(os.environ)
        for k in ("DARKBLOOM_BASE_URL", "DARKBLOOM_API_KEY", "QUEUE_AGENT_URL", "QUEUE_AGENT_TOKEN"):
            os.environ.pop(k, None)
        self.saved = (Q.DARKBLOOM_LOCAL_JSON, Q._darkbloom_served_models, Q._apply_fit_routing, Q._host_url_for,
                      Q._model_size_cached, Q._host_budget_or_zero)
        self.local = Path(_TMP) / "local.json"
        self.local.unlink(missing_ok=True)
        Q.DARKBLOOM_LOCAL_JSON = self.local            # absent: the container has no vendor file
        os.environ["DARKBLOOM_LOCAL_JSON"] = str(self.local)
        Q._darkbloom_served_models = lambda: {DB_MODEL}
        Q._apply_fit_routing = lambda job, w: ("unraid", "stub", False)
        Q._host_url_for = lambda w, n: w.KNOWN_OLLAMA_HOSTS.get(n)
        Q._model_size_cached = lambda w, m: 10 * 1024 ** 3
        Q._host_budget_or_zero = lambda w, n: 40 * 1024 ** 3
        qc._DB_REMOTE.update(rec={}, at=None, tried=None, agent=None)
        Q._DARKBLOOM_URL_GOOD[:] = [None, 0.0]

    def tearDown(self):
        (Q.DARKBLOOM_LOCAL_JSON, Q._darkbloom_served_models, Q._apply_fit_routing, Q._host_url_for,
         Q._model_size_cached, Q._host_budget_or_zero) = self.saved
        os.environ.clear(); os.environ.update(self.saved_env)

    def container(self):
        os.environ["QUEUE_AGENT_URL"] = self.url
        os.environ["QUEUE_AGENT_TOKEN"] = AGENT_TOKEN

    def lanes(self, pref, model=DB_MODEL):
        return Q._candidate_lanes({"id": "j", "model": model, "host_pref": pref}, FakeW)


class RemoteRecord(Base):
    def test_agent_key_with_configured_base(self):
        self.container()
        os.environ["DARKBLOOM_BASE_URL"] = ENV_BASE
        ANSWER["doc"] = {"api_key": AGENT_KEY}
        self.assertEqual(Q._darkbloom_url(), ENV_BASE)
        self.assertEqual(Q._darkbloom_record()["api_key"], AGENT_KEY)
        self.assertEqual(self.lanes("studio-db"), [ENV_BASE])
        self.assertEqual(SEEN[0], ("/v1/darkbloom-endpoint", "Bearer " + AGENT_TOKEN))

    def test_agent_relay_base_when_no_env_base(self):
        self.container()
        ANSWER["doc"] = {"api_key": AGENT_KEY, "base_url": RELAY_BASE}
        self.assertEqual(Q._darkbloom_url(), RELAY_BASE)
        self.assertEqual(self.lanes("auto"), [RELAY_BASE])

    def test_env_base_beats_agent_base(self):
        self.container()
        os.environ["DARKBLOOM_BASE_URL"] = ENV_BASE
        ANSWER["doc"] = {"api_key": AGENT_KEY, "base_url": RELAY_BASE}
        self.assertEqual(Q._darkbloom_url(), ENV_BASE)

    def test_agent_key_beats_stale_env_key(self):
        self.container()
        os.environ["DARKBLOOM_API_KEY"] = ENV_KEY
        os.environ["DARKBLOOM_BASE_URL"] = ENV_BASE
        ANSWER["doc"] = {"api_key": AGENT_KEY}
        self.assertEqual(Q._darkbloom_record()["api_key"], AGENT_KEY)
        self.assertEqual(qc.darkbloom_endpoint(), (ENV_BASE, AGENT_KEY))

    def test_answer_cached_between_lookups(self):
        self.container()
        ANSWER["doc"] = {"api_key": AGENT_KEY, "base_url": RELAY_BASE}
        for _ in range(5):
            Q._darkbloom_url()
        self.assertEqual(len(SEEN), 1)

    def test_no_network_when_refresh_is_off(self):
        # the state lock is held: the last answer stands, nothing is asked
        self.container()
        ANSWER["doc"] = {"api_key": AGENT_KEY, "base_url": RELAY_BASE}
        self.assertEqual(qc.darkbloom_remote_record(refresh=False), {})
        self.assertEqual(SEEN, [])
        qc.darkbloom_remote_record()
        self.assertEqual(qc.darkbloom_remote_record(refresh=False)["api_key"], AGENT_KEY)

    def test_key_change_is_picked_up_after_ttl(self):
        self.container()
        ANSWER["doc"] = {"api_key": AGENT_KEY, "base_url": RELAY_BASE}
        self.assertEqual(qc.darkbloom_remote_record(now=100.0)["api_key"], AGENT_KEY)
        ANSWER["doc"] = {"api_key": AGENT_KEY + "-2", "base_url": RELAY_BASE}
        self.assertEqual(qc.darkbloom_remote_record(now=110.0)["api_key"], AGENT_KEY)
        self.assertEqual(qc.darkbloom_remote_record(now=100.0 + qc.DARKBLOOM_REMOTE_TTL_S + 1)["api_key"], AGENT_KEY + "-2")


class AgentDown(Base):
    def test_falls_back_to_env(self):
        self.container()
        os.environ["DARKBLOOM_BASE_URL"] = ENV_BASE
        os.environ["DARKBLOOM_API_KEY"] = ENV_KEY
        self.assertEqual(Q._darkbloom_url(), ENV_BASE)           # fake agent answers 503
        self.assertEqual(Q._darkbloom_record()["api_key"], ENV_KEY)

    def test_env_unset_pinned_studio_db_holds_and_auto_goes_to_unraid(self):
        self.container()
        self.assertIsNone(Q._darkbloom_url())
        for pref in ("studio-db", "darkbloom"):
            self.assertEqual(self.lanes(pref), [], pref)
        self.assertEqual(self.lanes("auto", model="qwen3:14b"), [UNRAID])
        self.assertEqual(self.lanes("unraid", model="qwen3:14b"), [UNRAID])

    def test_down_agent_asked_once_per_retry_window(self):
        self.container()
        for _ in range(5):
            qc.darkbloom_remote_record(now=50.0)
        self.assertEqual(len(SEEN), 1)
        qc.darkbloom_remote_record(now=50.0 + qc.DARKBLOOM_REMOTE_RETRY_S + 1)
        self.assertEqual(len(SEEN), 2)

    def test_last_good_answer_survives_a_short_outage_then_expires(self):
        self.container()
        ANSWER["doc"] = {"api_key": AGENT_KEY, "base_url": RELAY_BASE}
        qc.darkbloom_remote_record(now=100.0)
        del ANSWER["doc"]
        t = 100.0 + qc.DARKBLOOM_REMOTE_TTL_S + 1
        self.assertEqual(qc.darkbloom_remote_record(now=t)["api_key"], AGENT_KEY)
        t = 100.0 + qc.DARKBLOOM_REMOTE_TTL_S + qc.DARKBLOOM_REMOTE_STALE_S + 1
        self.assertEqual(qc.darkbloom_remote_record(now=t), {})

    def test_agent_without_a_key_changes_nothing(self):
        self.container()
        os.environ["DARKBLOOM_BASE_URL"] = ENV_BASE
        os.environ["DARKBLOOM_API_KEY"] = ENV_KEY
        ANSWER["doc"] = {"generated_at": 1.0}                   # provider restarting: local.json had no key
        self.assertEqual(Q._darkbloom_record()["api_key"], ENV_KEY)


class NeverLogged(Base):
    def test_no_key_or_token_in_any_output(self):
        buf, err, logs = io.StringIO(), io.StringIO(), io.StringIO()
        h = logging.StreamHandler(logs)
        logging.getLogger().addHandler(h)
        self.addCleanup(logging.getLogger().removeHandler, h)
        self.container()
        os.environ["DARKBLOOM_API_KEY"] = ENV_KEY
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            ANSWER["doc"] = {"api_key": AGENT_KEY, "base_url": RELAY_BASE}
            Q._darkbloom_url(); Q._darkbloom_record(); qc.darkbloom_endpoint()
            qc._DB_REMOTE.update(at=None, tried=None)
            del ANSWER["doc"]                                    # agent down
            Q._darkbloom_url(); self.lanes("studio-db")
            qc._DB_REMOTE.update(at=None, tried=None)
            ANSWER["doc"] = {"api_key": 12345, "base_url": ["x"]}   # garbage
            Q._darkbloom_url()
        out = buf.getvalue() + err.getvalue() + logs.getvalue()
        for secret in (AGENT_KEY, ENV_KEY, AGENT_TOKEN):
            self.assertNotIn(secret, out)

    def test_garbage_answer_is_ignored(self):
        self.container()
        ANSWER["doc"] = {"api_key": 12345, "base_url": ["x"]}
        self.assertEqual(qc.darkbloom_remote_record(), {})


class MacUnchanged(Base):
    def test_agent_unset_reads_local_json_only(self):
        ANSWER["doc"] = {"api_key": AGENT_KEY, "base_url": RELAY_BASE}
        self.local.write_text(json.dumps({"api_key": "local-key-not-real", "base_url": "http://127.0.0.1:8000/v1"}))
        rec = Q._darkbloom_record()
        self.assertEqual(rec, {"api_key": "local-key-not-real", "base_url": "http://127.0.0.1:8000/v1"})
        self.assertEqual(Q._darkbloom_url(), "http://127.0.0.1:8000")
        self.assertEqual(SEEN, [])                                # never asked

    def test_apply_env_returns_the_same_object_when_nothing_is_set(self):
        rec = {"api_key": "k"}
        self.assertIs(qc.darkbloom_apply_env(rec), rec)
        self.assertEqual(qc.darkbloom_remote_record(), {})

    def test_agent_answer_is_laid_over_the_file_record(self):
        self.container()
        self.local.write_text(json.dumps({"api_key": "local-key-not-real", "pid": 7}))
        ANSWER["doc"] = {"api_key": AGENT_KEY, "base_url": RELAY_BASE}
        rec = Q._darkbloom_record()
        self.assertEqual((rec["api_key"], rec["base_url"], rec["pid"]), (AGENT_KEY, RELAY_BASE, 7))


if __name__ == "__main__":
    unittest.main()
