#!/usr/bin/env python3
"""Tests for queue_config: env order, TOML lookup, caching, state_dir."""
import os, sys, tempfile, time, unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import queue_config as qc


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "c.toml"
        self.saved = dict(os.environ)
        for k in list(os.environ):
            if k.startswith("QUEUE_") or k == "LEGACY_X":
                del os.environ[k]
        os.environ["QUEUE_CONFIG"] = str(self.cfg)

    def tearDown(self):
        os.environ.clear(); os.environ.update(self.saved)
        self.tmp.cleanup()

    def test_default_when_nothing(self):
        self.assertIsNone(qc.get("a.b"))
        self.assertEqual(qc.get("a.b", "d"), "d")

    def test_toml_dotted(self):
        self.cfg.write_text('[ollama]\nstudio_url = "http://x:1"\n[a.b]\nc = 3\n')
        self.assertEqual(qc.get("ollama.studio_url"), "http://x:1")
        self.assertEqual(qc.get("a.b.c"), 3)
        self.assertEqual(qc.get("a.b.missing", 7), 7)
        self.assertEqual(qc.get("ollama.studio_url.deeper", 9), 9)

    def test_env_beats_toml_legacy_beats_queue(self):
        self.cfg.write_text('[ollama]\nstudio_url = "file"\n')
        self.assertEqual(qc.get("ollama.studio_url"), "file")
        os.environ["QUEUE_OLLAMA_STUDIO_URL"] = "queue-env"
        self.assertEqual(qc.get("ollama.studio_url"), "queue-env")
        os.environ["LEGACY_X"] = "legacy"
        self.assertEqual(qc.get("ollama.studio_url", env="LEGACY_X"), "legacy")
        self.assertEqual(qc.get("ollama.studio_url", env=("NOPE", "LEGACY_X")), "legacy")

    def test_empty_env_is_unset(self):
        self.cfg.write_text('k = "file"\n')
        os.environ["QUEUE_K"] = ""
        self.assertEqual(qc.get("k"), "file")

    def test_reload_on_mtime_change(self):
        self.cfg.write_text('k = "one"\n')
        self.assertEqual(qc.get("k"), "one")
        self.cfg.write_text('k = "two"\n')
        os.utime(self.cfg, ns=(time.time_ns() + 5_000_000_000,) * 2)
        self.assertEqual(qc.get("k"), "two")
        self.cfg.unlink()
        self.assertIsNone(qc.get("k"))

    def test_bad_toml_falls_back(self):
        self.cfg.write_text('k = = broken\n')
        self.assertEqual(qc.get("k", "dflt"), "dflt")

    def test_typed_helpers(self):
        self.cfg.write_text('n = 5\nflag = true\nl = ["a", "b"]\n')
        self.assertEqual(qc.get_int("n"), 5)
        self.assertEqual(qc.get_int("zz", 4), 4)
        self.assertTrue(qc.get_bool("flag"))
        os.environ["QUEUE_FLAG"] = "0"
        self.assertFalse(qc.get_bool("flag", True))
        self.assertEqual(qc.get_list("l"), ["a", "b"])
        os.environ["QUEUE_L"] = "x, y ,,z"
        self.assertEqual(qc.get_list("l"), ["x", "y", "z"])
        self.assertEqual(qc.get_str("nokey", "e"), "e")

    def test_state_dir(self):
        self.assertEqual(qc.state_dir(), Path.home() / "bin")
        os.environ["QUEUE_STATE_DIR"] = "/tmp/qs"
        self.assertEqual(qc.state_dir(), Path("/tmp/qs"))

    def test_handoff_dir(self):
        os.environ.pop("HANDOFF_DIR", None)
        self.assertEqual(qc.handoff_dir(), Path.home() / "Desktop" / "GitHub Projects" / "ollama" / "ollama-handoff")
        self.cfg.write_text('[paths]\nhandoff_dir = "/srv/handoff"\n')
        self.assertEqual(qc.handoff_dir(), Path("/srv/handoff"))
        os.environ["HANDOFF_DIR"] = "/tmp/ho"
        self.assertEqual(qc.handoff_dir(), Path("/tmp/ho"))
        os.environ["HANDOFF_DIR"] = ""                      # empty counts as unset
        self.assertEqual(qc.handoff_dir(), Path("/srv/handoff"))

    def test_default_config_path(self):
        del os.environ["QUEUE_CONFIG"]
        self.assertEqual(qc.config_path(), Path.home() / ".config" / "ollama-queue" / "config.toml")

class DarkbloomApiTests(unittest.TestCase):
    KEYS = ("DARKBLOOM_BASE_URL", "DARKBLOOM_API_KEY", "DARKBLOOM_LOCAL_JSON", "QUEUE_API", "QUEUE_API_URL",
            "QUEUE_API_TOKEN", "QUEUE_AGENT_URL", "QUEUE_AGENT_TOKEN", "QUEUE_CONFIG", "HOME")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.saved = dict(os.environ)
        for k in self.KEYS:
            os.environ.pop(k, None)
        os.environ["HOME"] = str(self.dir)
        os.environ["QUEUE_CONFIG"] = str(self.dir / "c.toml")
        os.environ["DARKBLOOM_LOCAL_JSON"] = str(self.dir / "local.json")

    def tearDown(self):
        os.environ.clear(); os.environ.update(self.saved)
        self.tmp.cleanup()

    def _lj(self, **kw):
        import json
        (self.dir / "local.json").write_text(json.dumps(kw))

    def test_default(self):
        self.assertEqual(qc.darkbloom_endpoint(), ("http://127.0.0.1:8000", None))

    def test_config_base_no_key(self):
        (self.dir / "c.toml").write_text('[darkbloom]\nbase_url = "http://192.0.2.5:8000/"\n')
        self.assertEqual(qc.darkbloom_endpoint(), ("http://192.0.2.5:8000", None))

    def test_local_json_beats_config(self):
        (self.dir / "c.toml").write_text('[darkbloom]\nbase_url = "http://192.0.2.5:8000"\n')
        self._lj(base_url="http://192.0.2.6:8000/", api_key="k-file")
        self.assertEqual(qc.darkbloom_endpoint(), ("http://192.0.2.6:8000", "k-file"))

    def test_env_beats_file(self):
        self._lj(base_url="http://192.0.2.6:8000", api_key="k-file")
        os.environ["DARKBLOOM_BASE_URL"] = "http://192.0.2.7:8000"
        os.environ["DARKBLOOM_API_KEY"] = "k-env"
        self.assertEqual(qc.darkbloom_endpoint(), ("http://192.0.2.7:8000", "k-env"))

    def test_env_base_with_file_key(self):
        self._lj(base_url="http://127.0.0.1:8000", api_key="k-file")
        os.environ["DARKBLOOM_BASE_URL"] = "http://192.0.2.7:8000/"
        self.assertEqual(qc.darkbloom_endpoint(), ("http://192.0.2.7:8000", "k-file"))

    def test_apply_env_noop_returns_same_object(self):
        rec = {"base_url": "http://127.0.0.1:8000/v1", "api_key": "k"}
        self.assertIs(qc.darkbloom_apply_env(rec), rec)
        os.environ["DARKBLOOM_BASE_URL"] = "http://192.0.2.7:8000"
        out = qc.darkbloom_apply_env(rec, v1=True)
        self.assertEqual(out["base_url"], "http://192.0.2.7:8000/v1")
        self.assertEqual(out["api_key"], "k")
        self.assertEqual(rec["base_url"], "http://127.0.0.1:8000/v1")   # input not mutated

    def test_lan_base_is_darkbloom_host(self):
        self.assertTrue(qc.is_darkbloom_host("http://127.0.0.1:8000/v1"))
        self.assertFalse(qc.is_darkbloom_host("http://192.0.2.7:8000/v1"))
        os.environ["DARKBLOOM_BASE_URL"] = "http://192.0.2.7:8000"
        self.assertTrue(qc.is_darkbloom_host("http://192.0.2.7:8000/v1"))
        self.assertFalse(qc.is_darkbloom_host("http://192.0.2.8:8000/v1"))

    def test_key_not_in_cli_output(self):
        import io, contextlib
        os.environ["DARKBLOOM_API_KEY"] = "SECRET-KEY-XYZ"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            for c in ("darkbloom-base", "queue-api", "bogus"):
                qc._main(["x", c])
        self.assertNotIn("SECRET-KEY-XYZ", buf.getvalue())

    def test_queue_api_default_and_layers(self):
        self.assertEqual(qc.queue_api(), ("http://127.0.0.1:7684", None))
        self.assertEqual(qc.queue_api_headers(), {})
        (self.dir / "c.toml").write_text('[queue]\napi_url = "http://192.0.2.9:7684/"\n')
        self.assertEqual(qc.queue_api()[0], "http://192.0.2.9:7684")
        os.environ["QUEUE_API_URL"] = "http://192.0.2.10:7684"
        self.assertEqual(qc.queue_api()[0], "http://192.0.2.10:7684")
        os.environ["QUEUE_API"] = "http://192.0.2.11:7684"
        self.assertEqual(qc.queue_api()[0], "http://192.0.2.11:7684")

    def test_queue_api_token_file_mode(self):
        d = self.dir / ".config" / "ollama-queue"
        d.mkdir(parents=True)
        f = d / "queue-api-token"
        f.write_text("tok-file\n")
        os.chmod(f, 0o644)
        self.assertIsNone(qc.queue_api()[1])
        self.assertEqual(qc.queue_api_headers(), {})
        os.chmod(f, 0o600)
        self.assertEqual(qc.queue_api()[1], "tok-file")
        self.assertEqual(qc.queue_api_headers(), {"Authorization": "Bearer tok-file"})
        os.environ["QUEUE_API_TOKEN"] = "tok-env"
        self.assertEqual(qc.queue_api()[1], "tok-env")

    def test_agent_url_only_from_env(self):
        self.assertIsNone(qc.agent_url())
        self.assertIsNone(qc.agent_get("/v1/hold", 1))          # unset: None, no network
        os.environ["QUEUE_AGENT_URL"] = "http://192.0.2.1:1/"
        self.assertEqual(qc.agent_url(), "http://192.0.2.1:1")



class AgentPostTests(unittest.TestCase):
    """agent_post(): JSON POST to the Mac status service with its bearer token; None on any failure."""
    KEYS = DarkbloomApiTests.KEYS

    def setUp(self):
        self.saved = dict(os.environ)
        for k in self.KEYS:
            os.environ.pop(k, None)
        self.seen = []

    def tearDown(self):
        os.environ.clear(); os.environ.update(self.saved)

    def _server(self, status=200, body=b'{"ok": true, "mode": "on"}'):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        seen = self.seen

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                seen.append((self.path, self.headers.get("Authorization"), self.headers.get("Content-Type"),
                             self.rfile.read(n)))
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return "http://127.0.0.1:%d" % srv.server_address[1]

    def test_unset_is_none_without_network(self):
        self.assertIsNone(qc.agent_post("/v1/bloomgauge-tick", {"busy": True}))

    def test_posts_json_with_the_token(self):
        import json
        os.environ["QUEUE_AGENT_URL"] = self._server() + "/"
        os.environ["QUEUE_AGENT_TOKEN"] = "fake-agent-token"
        self.assertEqual(qc.agent_post("/v1/bloomgauge-tick", {"busy": True, "target": None}, 2),
                         {"ok": True, "mode": "on"})
        path, auth, ctype, raw = self.seen[0]
        self.assertEqual((path, auth, ctype), ("/v1/bloomgauge-tick", "Bearer fake-agent-token", "application/json"))
        self.assertEqual(json.loads(raw), {"busy": True, "target": None})

    def test_failures_are_none(self):
        os.environ["QUEUE_AGENT_URL"] = self._server(status=401, body=b'{"error": "unauthorized"}')
        self.assertIsNone(qc.agent_post("/v1/x", {}, 2))
        os.environ["QUEUE_AGENT_URL"] = self._server(body=b"not json")
        self.assertIsNone(qc.agent_post("/v1/x", {}, 2))
        os.environ["QUEUE_AGENT_URL"] = self._server(body=b"[1, 2]")
        self.assertIsNone(qc.agent_post("/v1/x", {}, 2))
        self.assertIsNone(qc.agent_post("/v1/x", {"bad": object()}, 2))     # not JSON-able: no request
        os.environ["QUEUE_AGENT_URL"] = "http://127.0.0.1:9"                # nothing listens
        self.assertIsNone(qc.agent_post("/v1/x", {}, 1))


class QueueStateTests(unittest.TestCase):
    """load_queue_state(): the state file while the queue runs here, GET /api/state when it runs elsewhere."""
    KEYS = DarkbloomApiTests.KEYS + ("QUEUE_STATE_DIR",)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.saved = dict(os.environ)
        for k in self.KEYS:
            os.environ.pop(k, None)
        os.environ["HOME"] = str(self.dir)
        os.environ["QUEUE_CONFIG"] = str(self.dir / "c.toml")
        os.environ["QUEUE_STATE_DIR"] = str(self.dir / "state")
        (self.dir / "state").mkdir()
        self.seen = []

    def tearDown(self):
        os.environ.clear(); os.environ.update(self.saved)
        self.tmp.cleanup()

    def _server(self, status=200, body=b'{"jobs": [{"id": "r1"}]}'):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        seen = self.seen

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return "http://127.0.0.1:%d" % srv.server_address[1]

    def _remote(self):
        # the fake API listens on loopback, so pretend its host is another machine
        saved = qc.queue_is_remote
        qc.queue_is_remote = lambda: True
        self.addCleanup(setattr, qc, "queue_is_remote", saved)

    def test_is_remote_by_host(self):
        self.assertFalse(qc.queue_is_remote())                     # default 127.0.0.1
        for base, remote in (("http://localhost:7684", False), ("http://[::1]:7684", False),
                             ("http://127.0.0.2:7684", False), ("http://192.0.2.11:7684", True),
                             ("http://queue.example.test:7684", True)):
            os.environ["QUEUE_API"] = base
            self.assertEqual(qc.queue_is_remote(), remote, base)

    def test_local_reads_state_file(self):
        self.assertIsNone(qc.load_queue_state())                   # no file yet
        qc.queue_state_path().write_text('{"jobs": [{"id": "a"}]}')
        self.assertEqual(qc.load_queue_state()["jobs"][0]["id"], "a")
        qc.queue_state_path().write_text("[1, 2]")                  # not a dict
        self.assertIsNone(qc.load_queue_state())
        qc.queue_state_path().write_text("{half")
        self.assertIsNone(qc.load_queue_state())

    def test_explicit_path_is_a_plain_file_even_when_remote(self):
        self._remote()
        os.environ["QUEUE_API"] = "http://192.0.2.1:9"             # would hang/fail if it were asked
        f = self.dir / "fixture.json"
        f.write_text('{"jobs": []}')
        self.assertEqual(qc.load_queue_state(str(f)), {"jobs": []})
        self.assertIsNone(qc.load_queue_state(str(self.dir / "missing.json")))

    def test_remote_uses_api_with_token(self):
        os.environ["QUEUE_API"] = self._server()
        os.environ["QUEUE_API_TOKEN"] = "tok-not-real"
        self._remote()
        qc.queue_state_path().write_text('{"jobs": [{"id": "stale-local"}]}')
        self.assertEqual(qc.load_queue_state()["jobs"][0]["id"], "r1")
        self.assertEqual(qc.load_queue_state(str(qc.queue_state_path()))["jobs"][0]["id"], "r1")  # the default path too
        self.assertEqual(self.seen[0], ("/api/state", "Bearer tok-not-real"))

    def test_remote_failures_are_none(self):
        self._remote()
        for status, body in ((401, b"no"), (200, b"{bad"), (200, b"[1]")):
            os.environ["QUEUE_API"] = self._server(status, body)
            self.assertIsNone(qc.load_queue_state(), (status, body))
        os.environ["QUEUE_API"] = "http://127.0.0.1:9"              # nothing listens
        self.assertIsNone(qc.load_queue_state(timeout=1))

    def test_cli_queue_state(self):
        import contextlib, io
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            self.assertEqual(qc._main(["x", "queue-state"]), 1)
        qc.queue_state_path().write_text('{"jobs": []}')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(qc._main(["x", "queue-state"]), 0)
        self.assertEqual(buf.getvalue().strip(), '{"jobs": []}')

    def test_events_header_token_only_when_remote(self):
        import importlib.util
        os.environ["QUEUE_API_TOKEN"] = "tok-not-real"

        def header():
            spec = importlib.util.spec_from_file_location("qed_hdr_test", str(Path(qc.__file__).with_name("queue-events-deliver.py")))
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m.HEADER
        h = header()
        self.assertNotIn("Authorization", h)                      # loopback needs no token
        self.assertIn("http://127.0.0.1:7684/api/runs/<id>/clear", h)
        os.environ["QUEUE_API"] = "http://192.0.2.11:7684"
        h = header()
        self.assertIn('-H "Authorization: Bearer $(cat ~/.config/ollama-queue/queue-api-token)"', h)
        self.assertNotIn("tok-not-real", h)


class StudioGuardTests(unittest.TestCase):
    """L3: inside the container no loopback URL may stand for the studio host."""
    def setUp(self):
        self.saved = dict(os.environ)
        for k in ("QUEUE_IN_CONTAINER", "OLLAMA_STUDIO_URL", "QUEUE_OLLAMA_STUDIO_URL"):
            os.environ.pop(k, None)
        os.environ["QUEUE_CONFIG"] = "/nonexistent/none.toml"
        qc._cache.clear() if hasattr(qc, "_cache") and hasattr(qc._cache, "clear") else None

    def tearDown(self):
        os.environ.clear(); os.environ.update(self.saved)

    def test_host_default_unchanged(self):
        os.environ["QUEUE_IN_CONTAINER"] = "0"
        self.assertEqual(qc.studio_url(), "http://127.0.0.1:11434")

    def test_container_loopback_default_is_dropped(self):
        os.environ["QUEUE_IN_CONTAINER"] = "1"
        self.assertEqual(qc.studio_url(), "")

    def test_container_loopback_configured_is_dropped(self):
        os.environ["QUEUE_IN_CONTAINER"] = "1"
        for u in ("http://localhost:11434", "http://127.0.0.2:11434", "http://[::1]:11434"):
            os.environ["OLLAMA_STUDIO_URL"] = u
            self.assertEqual(qc.studio_url(), "", u)

    def test_container_real_host_is_kept(self):
        os.environ["QUEUE_IN_CONTAINER"] = "1"
        os.environ["OLLAMA_STUDIO_URL"] = "http://studio.example.test:11434/"
        self.assertEqual(qc.studio_url(), "http://studio.example.test:11434")

    def test_host_loopback_kept_outside_container(self):
        os.environ["QUEUE_IN_CONTAINER"] = "0"
        os.environ["OLLAMA_STUDIO_URL"] = "http://127.0.0.1:11500"
        self.assertEqual(qc.studio_url(), "http://127.0.0.1:11500")

    def test_in_container_env_wins_over_marker(self):
        os.environ["QUEUE_IN_CONTAINER"] = "0"
        self.assertFalse(qc.in_container())
        os.environ["QUEUE_IN_CONTAINER"] = "1"
        self.assertTrue(qc.in_container())


if __name__ == "__main__":
    unittest.main()
