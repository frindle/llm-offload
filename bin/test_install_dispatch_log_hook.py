#!/usr/bin/env python3
"""Tests for install-dispatch-log-hook.py against copies of the real reminder hooks and a settings.json with the
real hook layout. Run on claude-sandbox only: python3 -I test_installer.py <dir holding the two .sh copies>"""
import json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
INST = HERE / "install-dispatch-log-hook.py"
SRC = Path(sys.argv.pop(1)) if len(sys.argv) > 1 else Path.home() / ".claude" / "hooks"   # read only: copied
SETTINGS = {"env": {"X": "1"}, "hooks": {
    "PostToolUse": [{"matcher": "Agent", "hooks": [{"type": "command", "command": "~/.claude/hooks/vault-agent-dispatch-reminder.sh"}]},
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "~/.claude/hooks/vault-write-tracker.sh"}]}],
    "SubagentStop": [{"hooks": [{"type": "command", "command": "~/.claude/hooks/vault-subagent-stop-reminder.sh"}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "~/.claude/hooks/vault-staleness-reminder.sh"}]}]}}
NAMES = ("vault-agent-dispatch-reminder.sh", "vault-subagent-stop-reminder.sh")


class T(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        (self.home / ".claude" / "hooks").mkdir(parents=True)
        (self.home / "bin").mkdir()
        (self.home / "bin" / "dispatch-log-hook.py").write_text("#")
        (self.home / ".claude" / "settings.json").write_text(json.dumps(SETTINGS, indent=2))
        for n in NAMES:
            shutil.copy2(SRC / n, self.home / ".claude" / "hooks" / n)
        self.orig = {p: p.read_bytes() for p in self.files()}

    def files(self):
        return [self.home / ".claude" / "settings.json"] + [self.home / ".claude" / "hooks" / n for n in NAMES]

    def run_inst(self, *args, ok=True):
        r = subprocess.run([sys.executable, "-I", str(INST), *args], capture_output=True, text=True,
                           env=dict(os.environ, HOME=str(self.home)), timeout=30)
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def hook_out(self, name, ev):
        r = subprocess.run(["bash", str(self.home / ".claude" / "hooks" / name)], input=json.dumps(ev),
                           capture_output=True, text=True, env=dict(os.environ, HOME=str(self.home)), timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_install_idempotent_uninstall(self):
        self.run_inst("--dry-run")
        self.assertEqual({p: p.read_bytes() for p in self.files()}, self.orig)   # dry run writes nothing
        self.assertIn("updated", self.run_inst().stdout)
        s = json.loads((self.home / ".claude" / "settings.json").read_text())
        cmds = lambda ev: [h["command"] for e in s["hooks"][ev] for h in e["hooks"]]
        self.assertEqual(cmds("PostToolUse").count("python3 -I ~/bin/dispatch-log-hook.py"), 1)
        self.assertEqual(cmds("SubagentStop").count("python3 -I ~/bin/dispatch-log-hook.py"), 1)
        ent = [h for e in s["hooks"]["PostToolUse"] if e.get("matcher") == "Agent" for h in e["hooks"]]
        self.assertEqual(ent[0]["command"], "~/.claude/hooks/vault-agent-dispatch-reminder.sh")   # old one kept first
        self.assertTrue(ent[1]["async"])
        self.assertEqual(s["env"], {"X": "1"}); self.assertIn("Stop", s["hooks"])
        after = {p: p.read_bytes() for p in self.files()}
        self.assertIn("already installed", self.run_inst().stdout)
        self.assertEqual({p: p.read_bytes() for p in self.files()}, after)
        for p in self.files():
            self.assertTrue(p.with_name(p.name + ".bak-dlog").exists())
        self.assertTrue(os.access(self.home / ".claude" / "hooks" / NAMES[0], os.X_OK))
        self.run_inst("--uninstall")
        self.assertEqual({p: p.read_bytes() for p in self.files()}, self.orig)

    def test_dispatch_hook_quiet_but_marks_durable(self):
        self.run_inst()
        out = self.hook_out(NAMES[0], {"session_id": "s1", "tool_input": {"description": "x"}})
        self.assertEqual(out, {})
        self.assertTrue((self.home / ".claude" / "state" / "vault-sess-s1-durable").exists())

    def test_stop_hook_keeps_findings_and_subagent_text(self):
        self.run_inst()
        out = self.hook_out(NAMES[1], {"agent_id": "a1"})
        txt = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("file the actual findings in the relevant project/topic vault note", txt)
        self.assertIn("written automatically by dispatch-log-hook.py; do not write it yourself", txt)
        self.assertIn("ONLY THE FINAL RESULT STRING", txt)
        self.assertNotIn("file BOTH", txt)

    def test_unexpected_text_changes_nothing(self):
        p = self.home / ".claude" / "hooks" / NAMES[1]
        p.write_text(p.read_text().replace("file BOTH", "file both"))
        before = {q: q.read_bytes() for q in self.files()}
        r = self.run_inst(ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual({q: q.read_bytes() for q in self.files()}, before)

    def test_missing_hook_script_refuses(self):
        (self.home / "bin" / "dispatch-log-hook.py").unlink()
        self.assertNotEqual(self.run_inst(ok=False).returncode, 0)
        self.assertEqual({p: p.read_bytes() for p in self.files()}, self.orig)


if __name__ == "__main__":
    unittest.main(verbosity=1)
