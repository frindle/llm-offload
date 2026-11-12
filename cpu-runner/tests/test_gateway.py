"""default_gateway(): the isolation self-test probes this LAN's real router instead of a fixed address. No Docker."""
import os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent"))
import agent as agent_mod  # noqa: E402

HEAD = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"


class TestDefaultGateway(unittest.TestCase):
    def route(self, body):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as f:
            f.write(HEAD + body)
        self.addCleanup(os.unlink, path)
        return path

    def test_default_route(self):
        p = self.route("eth0\t000200C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
                       "eth0\t00000000\t010200C0\t0003\t0\t0\t0\t00000000\t0\t0\t0\n")
        self.assertEqual(agent_mod.default_gateway(p), "192.0.2.1")

    def test_no_default_route(self):
        p = self.route("eth0\t000200C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n")
        self.assertIsNone(agent_mod.default_gateway(p))

    def test_no_route_file(self):
        self.assertIsNone(agent_mod.default_gateway("/nonexistent/route"))

    def test_garbage_gateway(self):
        self.assertIsNone(agent_mod.default_gateway(self.route("eth0\t00000000\tZZZZ\t0003\n")))


if __name__ == "__main__":
    unittest.main()
