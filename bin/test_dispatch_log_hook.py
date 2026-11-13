#!/usr/bin/env python3
"""Tests for dispatch-log-hook.py. Run on claude-sandbox only: python3 -I test_dispatch_log_hook.py"""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE / "dispatch-log-hook.py"


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = Path(self.tmp) / "state"
        self.out = Path(self.tmp) / "vault.md"
        self.fake = Path(self.tmp) / "fake-append"
        self.fake.write_text("#!/bin/sh\n[ -f \"%s/down\" ] && exit 1\n[ \"$1\" = - ] || exit 2\ncat >> \"%s\"\n"
                             % (self.tmp, self.out))
        self.fake.chmod(0o755)
        self.trk = Path(self.tmp) / "tracker.jsonl"
        self.tracker = Path(self.tmp) / "fake-tracker"
        self.tracker.write_text("#!/bin/sh\ncat >> \"%s\"\necho >> \"%s\"\n" % (self.trk, self.trk))
        self.tracker.chmod(0o755)
        self.env = dict(os.environ, DISPATCH_LOG_APPEND=str(self.fake), DISPATCH_LOG_STATE_DIR=str(self.state),
                        DISPATCH_LOG_NOW="1000000", TZ="America/Los_Angeles", DISPATCH_LOG_TRACKER=str(self.tracker))

    def run_hook(self, ev, **env):
        e = dict(self.env, **env)
        r = subprocess.run([sys.executable, "-I", str(HOOK)], input=json.dumps(ev), capture_output=True, text=True,
                           env=e, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")          # nothing goes back into the conversation
        return r

    def lines(self):
        return self.out.read_text().strip().split("\n") if self.out.exists() else []

    def fg(self, content="Found it in src/x.py:12. Done."):
        return {"hook_event_name": "PostToolUse", "tool_name": "Agent", "session_id": "57408d61-aaaa",
                "cwd": "/Users/x/proj.nosync",
                "tool_input": {"description": "Find the parser", "subagent_type": "scout", "model": "haiku",
                               "prompt": "secret prompt text"},
                "tool_response": {"status": "completed", "agentId": "a1", "content": [{"type": "text", "text": content}],
                                  "resolvedModel": "claude-haiku-5-5", "totalTokens": 12345,
                                  "totalDurationMs": 83000, "totalToolUseCount": 7}}

    def bg(self, aid="b1"):
        return {"hook_event_name": "PostToolUse", "tool_name": "Agent", "session_id": "57408d61-aaaa",
                "cwd": "/Users/x/proj", "tool_input": {"description": "Long build", "subagent_type": "general-purpose",
                                                       "model": "sonnet", "run_in_background": True},
                "tool_response": {"status": "async_launched", "agentId": aid, "description": "Long build",
                                  "resolvedModel": "claude-sonnet-5-5", "outputFile": "/x/y.output"}}

    def stop(self, aid, msg="All 46 tests pass.", transcript=None):
        return {"hook_event_name": "SubagentStop", "session_id": "57408d61-aaaa", "cwd": "/Users/x/proj",
                "agent_id": aid, "agent_type": "general-purpose", "stop_hook_active": False,
                "agent_transcript_path": transcript or "/nonexistent", "last_assistant_message": msg}

    def test_foreground_one_line(self):
        self.run_hook(self.fg())
        ls = self.lines()
        self.assertEqual(len(ls), 1)
        l = ls[0]
        for want in ("[auto] proj.nosync (session 57408d61)", "ran scout/claude-haiku-5-5", "\"Find the parser\"",
                     "7 tool uses", "12.3k tokens", "1m23s", "Found it in src/x.py:12"):
            self.assertIn(want, l)
        self.assertNotIn("secret prompt text", l)

    def test_foreground_stop_skipped(self):
        self.run_hook(self.stop("a1"))           # SubagentStop fires before PostToolUse for foreground agents
        self.run_hook(self.fg())
        self.run_hook(self.stop("a1"))
        self.assertEqual(len(self.lines()), 1)

    def test_background_launch_and_finish(self):
        tr = Path(self.tmp) / "agent-b1.jsonl"
        rows = [{"message": {"id": "m1", "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000,
                                                   "output_tokens": 90}}},
                {"message": {"id": "m1", "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000,
                                                   "output_tokens": 90}}},   # same message twice: counted once
                {"message": {"id": "m2", "usage": {"cache_creation_input_tokens": 900}}}]
        tr.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
        self.run_hook(self.bg())
        self.run_hook(self.stop("b1", transcript=str(tr)), DISPATCH_LOG_NOW="1000125")
        ls = self.lines()
        self.assertEqual(len(ls), 2)
        self.assertIn("launched general-purpose/claude-sonnet-5-5 in background: \"Long build\"", ls[0])
        self.assertIn("background general-purpose/claude-sonnet-5-5 finished: \"Long build\"", ls[1])
        self.assertIn("2m05s since launch", ls[1])
        self.assertIn("2.0k tokens", ls[1])
        self.assertIn("All 46 tests pass.", ls[1])

    def test_resumed_background(self):
        self.run_hook(self.bg())
        self.run_hook(self.stop("b1"))
        self.run_hook(self.stop("b1", msg="second"))
        ls = self.lines()
        self.assertEqual(len(ls), 3)
        self.assertIn("finished again (resumed)", ls[2])

    def test_handback_pointer_replaced(self):
        ptr = ("This agent's report was delivered to you as a message from \"a5c5efd2f09c98174\" (its SubagentHandback "
               "call). Read it there; it is not repeated here.")
        self.run_hook(self.stop("a1", msg="Board is a 2.1 rev with ESP32-S3."))
        self.run_hook(self.fg(ptr))
        l = self.lines()[0]
        self.assertIn("Board is a 2.1 rev with ESP32-S3.", l)
        self.assertNotIn("a5c5efd2f09c98174", l)
        self.run_hook(self.fg(ptr))               # no stop recorded this time
        l = self.lines()[1]
        self.assertIn("(report sent as a separate message)", l)
        self.assertNotIn("a5c5efd2f09c98174", l)

    def test_unknown_stop_ignored(self):
        self.run_hook(self.stop("zz"))
        self.assertEqual(self.lines(), [])

    def test_other_tools_ignored(self):
        self.run_hook({"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"},
                       "tool_response": {"stdout": "x"}})
        self.assertEqual(self.lines(), [])

    def test_masking_and_cut(self):
        txt = ("Done. token=abcdef1234567890 Authorization: Bearer abc.def.ghijklmnop api_key: 'zzzzzzzzzz' "
               "ghp_ABCDEFGHIJKLMNOPQRST hash 0123456789abcdef0123456789abcdef0123 " + "word " * 100)
        self.run_hook(self.fg(txt))
        l = self.lines()[0]
        for bad in ("abcdef1234567890", "abc.def.ghijklmnop", "zzzzzzzzzz", "ghp_ABCDEFGHIJKLMNOPQRST",
                    "0123456789abcdef0123456789abcdef0123"):
            self.assertNotIn(bad, l)
        self.assertIn("<masked>", l)
        self.assertTrue(l.endswith("..."))
        self.assertLess(len(l), 600)

    def test_vault_down_spools_then_flushes(self):
        (Path(self.tmp) / "down").write_text("")
        self.run_hook(self.fg("first"))
        self.assertEqual(self.lines(), [])
        spool = self.state / "dispatch-log-spool.md"
        self.assertIn("first", spool.read_text())
        os.remove(Path(self.tmp) / "down")
        self.run_hook(self.fg("second"))
        ls = self.lines()
        self.assertEqual(len(ls), 2)
        self.assertIn("first", ls[0]); self.assertIn("second", ls[1])
        self.assertFalse(spool.exists())

    def test_missing_append_cmd_spools(self):
        self.run_hook(self.fg(), DISPATCH_LOG_APPEND=str(Path(self.tmp) / "nope"))
        self.assertTrue((self.state / "dispatch-log-spool.md").exists())

    def test_kill_switch(self):
        self.run_hook(self.fg(), DISPATCH_LOG_HOOK="off")
        self.assertEqual(self.lines(), [])

    def test_garbage_input(self):
        r = subprocess.run([sys.executable, "-I", str(HOOK)], input="not json", capture_output=True, text=True,
                           env=self.env, timeout=30)
        self.assertEqual(r.returncode, 0)
        r = subprocess.run([sys.executable, "-I", str(HOOK)], input=json.dumps(
            {"hook_event_name": "PostToolUse", "tool_name": "Agent", "tool_input": None, "tool_response": "plain text"}),
            capture_output=True, text=True, env=self.env, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertEqual(len(self.lines()), 1)

    def test_state_pruned(self):
        self.run_hook(self.bg("old"))
        self.run_hook(self.bg("new"), DISPATCH_LOG_NOW=str(1000000 + 8 * 86400))
        st = json.loads((self.state / "dispatch-log-state.json").read_text())
        self.assertNotIn("old", st); self.assertIn("new", st)

    def test_tracker_told_after_write_only(self):
        (Path(self.tmp) / "down").write_text("")
        self.run_hook(self.fg("first"))
        self.assertFalse(self.trk.exists())           # vault write failed: no tracker event
        os.remove(Path(self.tmp) / "down")
        self.run_hook(self.fg("second"))
        evs = [json.loads(x) for x in self.trk.read_text().split("\n") if x.strip()]
        self.assertEqual(len(evs), 1)
        e = evs[0]
        self.assertEqual(e["tool_name"], "Bash"); self.assertEqual(e["session_id"], "57408d61-aaaa")
        import re
        # the tracker's own vault-log-append pattern must match the command
        self.assertTrue(re.search(r"(^|[\s/])vault-log(-append)?\s", e["tool_input"]["command"]))

    def test_line_starting_with_dash_ok(self):
        self.run_hook(self.fg())
        self.assertTrue(self.lines()[0].startswith("- "))


if __name__ == "__main__":
    unittest.main(verbosity=1)
