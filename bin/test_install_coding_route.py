#!/usr/bin/env python3
"""Tests for install-coding-route.py against a copy of the real hook. Run on claude-sandbox only:
python3 -I test_install_coding_route.py <dir holding dispatch-to-qwen-reminder.sh>"""
import json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
INST = HERE / "install-coding-route.py"
SRC = Path(sys.argv.pop(1)) if len(sys.argv) > 1 else Path.home() / ".claude" / "hooks"   # read only: copied
NAME = "dispatch-to-qwen-reminder.sh"


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (SRC / NAME).exists():
            raise unittest.SkipTest("no %s under %s (pass the hooks dir as the first argument)" % (NAME, SRC))

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        (self.home / ".claude" / "hooks").mkdir(parents=True)
        self.hook = self.home / ".claude" / "hooks" / NAME
        shutil.copy2(SRC / NAME, self.hook)
        self.orig = self.hook.read_bytes()

    def run_inst(self, *args, ok=True):
        r = subprocess.run([sys.executable, "-I", str(INST), *args], capture_output=True, text=True,
                           env=dict(os.environ, HOME=str(self.home)), timeout=30)
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def hook_out(self, path):
        r = subprocess.run(["bash", str(self.hook)], input=json.dumps({"tool_input": {"file_path": path}}),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_install_idempotent_uninstall(self):
        self.run_inst("--dry-run")
        self.assertEqual(self.hook.read_bytes(), self.orig)
        self.assertIn("updated", self.run_inst().stdout)
        after = self.hook.read_bytes()
        self.assertIn("already installed", self.run_inst().stdout)
        self.assertEqual(self.hook.read_bytes(), after)
        self.assertTrue(os.access(self.hook, os.X_OK))
        self.run_inst("--uninstall")
        self.assertEqual(self.hook.read_bytes(), self.orig)

    def test_new_text_is_valid_json_and_complete(self):
        self.run_inst()
        out = json.loads(self.hook_out("/Users/x/proj/app.py"))
        txt = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        for want in ("ROUTING REMINDER: just edited /Users/x/proj/app.py directly", "Sonnet agent",
                     "$5.95", "exists before the job", "spot audit", "under about 40 lines", "pass silently."):
            self.assertIn(want, txt)
        self.assertNotIn("qwen should write coding work by default", txt)

    def test_exemptions_unchanged(self):
        self.run_inst()
        for p in ("/tmp/x.py", "/private/tmp/y.py", "/a/scratchpad/z.py", "/Users/x/notes.md"):
            self.assertEqual(self.hook_out(p), "")
        self.assertEqual(self.hook_out(""), "")

    def test_only_the_reminder_line_and_comment_change(self):
        self.run_inst()
        a, b = self.orig.decode().splitlines(), self.hook.read_text().splitlines()
        self.assertEqual(len(b), len(a) + 2)
        self.assertEqual([l for l in b if not l.startswith("# 2026-11-13") and not l.startswith("# model only")
                          and "ROUTING REMINDER" not in l],
                         [l for l in a if "DISPATCH REMINDER" not in l])

    def test_unexpected_text_changes_nothing(self):
        self.hook.write_text(self.hook.read_text().replace("Standing rule", "standing rule"))
        before = self.hook.read_bytes()
        r = self.run_inst(ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.hook.read_bytes(), before)
        self.assertFalse(self.hook.with_name(NAME + ".bak-route").exists())


if __name__ == "__main__":
    unittest.main(verbosity=1)
