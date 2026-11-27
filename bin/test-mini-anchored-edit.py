"""Tests for the worker's anchored edit tool, the mutation-recovery rule and the child-process env allowlist.

anchored_edit.py (view/edit with a content tag and line anchors), mutation_recovery.py (3 failed edits end the
attempt, one stale-tag retry is free) wired into the runner's failfast_reason, and mini_ext/child_env.py.
No model, no network. Plain unittest, so it runs with `python3 -m unittest` or pytest.

    python3 test-mini-anchored-edit.py
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "mini_ext"))
import anchored_edit as ae          # noqa: E402
import child_env                    # noqa: E402
import mutation_recovery as mr      # noqa: E402


def load_runner():
    sp = importlib.util.spec_from_file_location("mswe_anchored", HERE / "mini-swe-runner.py")
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        self._old = os.environ.get("ANCHOR_EDIT_STATE")
        os.environ["ANCHOR_EDIT_STATE"] = str(self.d / "state")
        self.f = str(self.d / "a.txt")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("ANCHOR_EDIT_STATE", None)
        else:
            os.environ["ANCHOR_EDIT_STATE"] = self._old
        self._tmp.cleanup()

    def write(self, text, name="a.txt"):
        p = self.d / name
        p.write_bytes(text.encode() if isinstance(text, str) else text)
        return str(p)

    def view(self, path=None, rng=None):
        rc, out = ae.cmd_view(path or self.f, rng)
        self.assertEqual(rc, 0, out)
        return out.split("tag=")[1][:4], out

    def edit(self, ops, tag, path=None):
        return ae.cmd_edit(path or self.f, tag, ops)


class TagTests(unittest.TestCase):
    def test_tag_ignores_line_endings_and_trailing_blanks(self):
        a = ae.decode(b"x = 1\r\ny = 2  \r\n")[2]
        b = ae.decode(b"x = 1\ny = 2\n")[2]
        self.assertEqual(ae.tag_of(a), ae.tag_of(b))
        self.assertRegex(ae.tag_of(a), r"^[0-9A-F]{4}$")

    def test_tag_changes_with_content(self):
        self.assertNotEqual(ae.tag_of(["a", "b"]), ae.tag_of(["a", "c"]))


class EditTests(Base):
    def test_replace_insert_delete_in_one_call_use_original_numbers(self):
        self.write("a\nb\nc\nd\ne\n")
        tag, out = self.view()
        self.assertIn("     2| b", out)
        rc, msg = self.edit("replace 2\n+B1\n+B2\ndelete 4\ninsert-after 5\n+tail\ninsert-after 0\n+head\n", tag)
        self.assertEqual(rc, 0, msg)
        self.assertEqual(Path(self.f).read_text(), "head\na\nB1\nB2\nc\ne\ntail\n")

    def test_after_an_insert_the_old_numbers_below_it_are_refused_not_applied_to_a_neighbour(self):
        self.write("".join("l%d\n" % i for i in range(1, 11)))
        tag, _ = self.view()
        rc, msg = self.edit("insert-after 2\n+X\n", tag)
        self.assertEqual(rc, 0, msg)
        tag2 = msg.split("tag=")[1][:4]
        rc, msg = self.edit("replace 8\n+L8\n", tag2)         # old line 8 is now line 9
        self.assertEqual(rc, 2)
        self.assertIn("EDIT-REFUSED[unseen]", msg)
        rc, msg = self.edit("replace 1\n+L1\n", tag2)         # above the insert: same number, same text
        self.assertEqual(rc, 0, msg)
        tag3 = msg.split("tag=")[1][:4]
        tag4, _ = self.view(rng="7-10")
        self.assertEqual(tag3, tag4)
        rc, msg = self.edit("replace 9\n+L8\n", tag4)
        self.assertEqual(rc, 0, msg)
        self.assertEqual(Path(self.f).read_text().splitlines()[8], "L8")

    def test_a_same_size_replace_keeps_the_lines_below_it_editable(self):
        self.write("".join("l%d\n" % i for i in range(1, 11)))
        tag, _ = self.view()
        rc, msg = self.edit("replace 2\n+L2\n", tag)
        tag2 = msg.split("tag=")[1][:4]
        rc, msg = self.edit("replace 9\n+L9\n", tag2)
        self.assertEqual(rc, 0, msg)
        self.assertEqual(Path(self.f).read_text().splitlines()[8], "L9")
        self.assertTrue(msg.startswith("EDIT-OK"))
        newtag = msg.split("tag=")[1][:4]
        self.assertNotEqual(newtag, tag)
        # the edit's own result is a view: the next edit can chain on the new tag without another view
        rc, msg = self.edit("replace 1\n+HEAD\n", newtag)
        self.assertEqual(rc, 0, msg)
        self.assertEqual(Path(self.f).read_text().splitlines()[0], "HEAD")

    def test_crlf_bom_and_missing_final_newline_are_preserved(self):
        self.write(b"\xef\xbb\xbfone\r\ntwo\r\nthree")
        tag, _ = self.view()
        rc, msg = self.edit("replace 2\n+TWO\n", tag)
        self.assertEqual(rc, 0, msg)
        self.assertEqual(Path(self.f).read_bytes(), b"\xef\xbb\xbfone\r\nTWO\r\nthree")

    def test_stale_tag_is_refused_and_says_view_again(self):
        self.write("a\nb\n")
        tag, _ = self.view()
        Path(self.f).write_text("a\nb\nc\n")          # the file moved under the model
        rc, msg = self.edit("replace 1\n+x\n", tag)
        self.assertEqual(rc, 2)
        self.assertIn("EDIT-REFUSED[stale]", msg)
        self.assertIn("view", msg)
        self.assertEqual(Path(self.f).read_text(), "a\nb\nc\n")

    def test_never_viewed_file_is_refused(self):
        self.write("a\nb\n")
        tag = ae.tag_of(["a", "b"])                   # a correct guess is still not provenance
        rc, msg = self.edit("replace 1\n+x\n", tag)
        self.assertEqual(rc, 2)
        self.assertIn("EDIT-REFUSED[unseen]", msg)

    def test_anchor_outside_the_viewed_range_is_refused(self):
        self.write("".join(f"l{i}\n" for i in range(1, 31)))
        tag, _ = self.view(rng="1-10")
        rc, msg = self.edit("replace 20\n+x\n", tag)
        self.assertEqual(rc, 2)
        self.assertIn("EDIT-REFUSED[unseen]", msg)
        self.assertIn("20", msg)
        # a second view of the same version adds to what was seen (read fusion)
        tag2, _ = self.view(rng="15-25")
        self.assertEqual(tag, tag2)
        rc, msg = self.edit("replace 20\n+x\n", tag)
        self.assertEqual(rc, 0, msg)

    def test_clipped_line_is_not_anchorable(self):
        self.write("short\n" + "x" * 3000 + "\nend\n")
        tag, out = self.view()
        self.assertIn("clipped", out)
        rc, msg = self.edit("replace 2\n+y\n", tag)
        self.assertIn("EDIT-REFUSED[unseen]", msg)

    def test_bad_ops_are_refused_without_writing(self):
        text = "a\nb\nc\n"
        self.write(text)
        tag, _ = self.view()
        for ops in ("replace 2\n", "replace 1-2\n+x\ndelete 2\n", "replace 9\n+x\n", "frobnicate 1\n", "+x\n", "",
                    "delete 2\n+x\n", "replace 1\n+a\n"):
            rc, msg = self.edit(ops, tag)
            self.assertEqual(rc, 2, ops)
            self.assertIn("EDIT-REFUSED[invalid]", msg, ops)
            self.assertEqual(Path(self.f).read_text(), text)

    def test_syntax_regression_is_refused_by_edit_guard(self):
        p = self.write("def f():\n    return 1\n", "m.py")
        tag, _ = self.view(p)
        rc, msg = self.edit("replace 2\n+    return (\n", tag, p)
        self.assertEqual(rc, 2)
        self.assertIn("EDIT-REFUSED[guard]", msg)
        self.assertEqual(Path(p).read_text(), "def f():\n    return 1\n")

    def test_cli_round_trip(self):
        self.write("a\nb\n")
        env = dict(os.environ)
        out = subprocess.run([sys.executable, str(HERE / "anchored_edit.py"), "view", self.f], capture_output=True, text=True, env=env).stdout
        tag = out.split("tag=")[1][:4]
        r = subprocess.run([sys.executable, str(HERE / "anchored_edit.py"), "edit", self.f, tag], input="replace 2\n+B\n",
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(Path(self.f).read_text(), "a\nB\n")
        r = subprocess.run([sys.executable, str(HERE / "anchored_edit.py"), "edit", self.f, tag], input="replace 2\n+C\n",
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("EDIT-REFUSED[stale]", r.stdout)


def step(kind, n=0):
    """A (command, output) step of the kind named."""
    edit = "python3 anchored_edit.py edit src/a.py 1A2B <<'EOF'\nreplace 3\n+x\nEOF"
    return {
        "ok": (edit, "EDIT-OK src/a.py tag=9F00"),
        "stale": (edit, "EDIT-REFUSED[stale]: tag 1A2B does not match"),
        "unseen": (edit, f"EDIT-REFUSED[unseen]: line(s) 3 were never shown ({n})"),
        "patch": ("git apply fix.diff", "error: patch failed: src/a.py:10\nerror: src/a.py: patch does not apply"),
        "read": (f"nl -ba src/a.py | sed -n '{n},{n + 9}p'", f"out{n}"),
    }[kind]


class MutationRecoveryTests(unittest.TestCase):
    def run_kinds(self, *kinds):
        return mr.mutation_reason([step(k, i) for i, k in enumerate(kinds)])

    def test_first_stale_is_free_then_failures_count(self):
        self.assertIsNone(self.run_kinds("stale", "unseen", "unseen"))      # 1 free + 2 counted
        self.assertIsNone(self.run_kinds("stale", "stale", "stale"))        # 1 free + 2 counted
        self.assertIn("3 edits failed", self.run_kinds("stale", "stale", "stale", "stale"))

    def test_three_non_stale_failures_end_the_attempt(self):
        why = self.run_kinds("unseen", "read", "unseen", "unseen")
        self.assertIn("fail-fast", why)
        self.assertIn("view the file again", why)

    def test_good_edit_resets_the_count_and_the_free_retry(self):
        self.assertIsNone(self.run_kinds("unseen", "unseen", "ok", "unseen", "unseen"))
        self.assertIsNone(self.run_kinds("stale", "unseen", "unseen", "ok", "stale", "unseen", "unseen"))

    def test_repeated_shell_patch_advice(self):
        why = self.run_kinds("patch", "patch", "patch")
        self.assertIn("do not repeat a shell patch command", why)
        self.assertNotIn("view the file again", why)

    def test_a_patch_that_applied_and_unrelated_commands_are_not_failures(self):
        ok_patch = ("git apply fix.diff", "")
        steps = [ok_patch] * 5 + [("grep -rn patch .", "x: does not apply")] * 5 + [("ls", "")] * 5
        self.assertIsNone(mr.mutation_reason(steps))

    def test_a_patch_that_applied_resets_the_count(self):
        ok_patch = ("git apply fix.diff", "")
        self.assertIsNone(mr.mutation_reason([step("patch"), step("patch"), ok_patch, step("patch"), step("patch")]))

    def test_failfast_reason_in_the_runner_uses_it(self):
        R = load_runner()
        spec, steps = {}, [step("read", i) for i in range(2)]
        self.assertIsNone(R.failfast_reason(steps, "D", spec, "."))
        why = R.failfast_reason(steps + [step("unseen", i) for i in range(3)], "D", spec, ".")
        self.assertIsNotNone(why)
        self.assertIn("3 edits failed", why)
        # the old loop-breaker is still in force
        self.assertIn("3 times in a row", R.failfast_reason([("ls", "o")] * 3, "D", spec, "."))

    def test_runner_help_names_the_tool_and_is_opt_in(self):
        R = load_runner()
        self.assertIn("anchored_edit.py", R.anchored_edit_help())
        self.assertNotIn(str(Path.home()), R.anchored_edit_help().replace(str(HERE), ""))


class ChildEnvTests(unittest.TestCase):
    SECRETS = {"OPENAI_API_KEY": "k1", "GITHUB_TOKEN": "t1", "AWS_SECRET_ACCESS_KEY": "s1", "MINI_KEY_FILE": "/x", "SSH_AUTH_SOCK": "/s"}

    def test_allowlist_keeps_the_basics_and_drops_secrets(self):
        env = {"PATH": "/bin", "HOME": "/h", "LC_ALL": "C", "ANCHOR_EDIT_STATE": "/s", "DISPATCH_VERIFY_SANDBOX": "1", "PAGER": "cat",
               **self.SECRETS}
        got = child_env.minimal_env(env)
        self.assertEqual(set(got), {"PATH", "HOME", "LC_ALL", "ANCHOR_EDIT_STATE", "DISPATCH_VERIFY_SANDBOX", "PAGER"})

    def test_install_wraps_the_local_runner(self):
        seen = []
        mod = types.ModuleType("minisweagent.environments.local")
        mod._run = lambda command, cwd, env, timeout: seen.append(dict(env)) or "done"
        pkg, envs = types.ModuleType("minisweagent"), types.ModuleType("minisweagent.environments")
        envs.local = mod
        saved = {k: sys.modules.get(k) for k in ("minisweagent", "minisweagent.environments", "minisweagent.environments.local")}
        sys.modules.update({"minisweagent": pkg, "minisweagent.environments": envs, "minisweagent.environments.local": mod})
        try:
            self.assertTrue(child_env.install())
            self.assertEqual(mod._run("env", "/", {"PATH": "/bin", "GITHUB_TOKEN": "t"}, 5), "done")
            self.assertEqual(seen, [{"PATH": "/bin"}])
            del mod._run
            self.assertFalse(child_env.install())     # a mini without `_run`: nothing changes, no crash
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    def test_a_real_shell_command_sees_no_secret(self):
        env = child_env.minimal_env({**os.environ, **self.SECRETS})
        out = subprocess.run("env; python3 -c 'print(1)' && git --version", shell=True, capture_output=True, text=True, env=env).stdout
        self.assertNotIn("GITHUB_TOKEN", out)
        self.assertNotIn("OPENAI_API_KEY", out)
        self.assertIn("git version", out)             # the tools a test run needs still resolve

    def test_launcher_stub_installs_it_only_when_asked(self):
        R = load_runner()
        self.assertIn("MINI_CHILD_ENV", R.LAUNCH)
        self.assertIn("child_env.install()", R.LAUNCH)


if __name__ == "__main__":
    unittest.main()
