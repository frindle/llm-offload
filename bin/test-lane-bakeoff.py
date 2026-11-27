import importlib.util, unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
ld = SourceFileLoader("lb", str(Path(__file__).parent / "lane-bakeoff"))
B = importlib.util.module_from_spec(importlib.util.spec_from_loader("lb", ld)); ld.exec_module(B)
class T(unittest.TestCase):
    def test_review_stats(self):
        rows = [dict(truth="reject", pred="reject", wall_s=1), dict(truth="reject", pred="ok", wall_s=3),
                dict(truth="ok", pred="ok", wall_s=2), dict(truth="ok", pred="reject", wall_s=2), dict(truth="ok", error="x")]
        s = B.review_stats(rows)
        self.assertEqual((s["reject_recall"], s["false_reject"], s["errors"]), (50.0, 33.3, 1)); self.assertEqual(s["parse_rate"], 80.0)
    def test_triage_stats(self):
        s = B.triage_stats([dict(truth="send", pred="send", kind="a"), dict(truth="refuse", pred="send", kind="b")])
        self.assertEqual(s["accuracy"], 50.0); self.assertEqual(s["by_kind"]["a"], "1/1")
    def test_safe_and_score(self):
        self.assertEqual(B.safe("unraid:gemma4:12b-it"), "unraid_gemma4_12b-it")
        self.assertGreater(B.score({"review": {"reject_recall": 80, "agreement": 70, "false_reject": 10}}),
                           B.score({"review": {"reject_recall": 20, "agreement": 90, "false_reject": 0}}))
    def test_never_touches_records(self):
        src = Path(B.__file__ if hasattr(B, "__file__") else "").read_text() if False else Path(__file__).with_name("lane-bakeoff").read_text()
        self.assertNotIn("write_text(json.dumps(rows", src); self.assertNotIn("mini-auto\"), \"review", src)
if __name__ == "__main__": unittest.main()
