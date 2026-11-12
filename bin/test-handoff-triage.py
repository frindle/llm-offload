#!/usr/bin/env python3
"""Offline tests for handoff-triage.py (no Darkbloom, no handoff writes)."""
import importlib.util, sys, unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("ht", Path(__file__).with_name("handoff-triage.py"))
ht = importlib.util.module_from_spec(spec); spec.loader.exec_module(ht)

PAGE = "# x\n- **status**: `done`  (exit 0)\nline two\n"


def item(**kw):
    base = dict(id="a", label="foo", status="done", gate="PASS", files=1, not_checked=0,
                untrusted=False, code_high=0, cwd="/nonexistent", awaiting_signoff=False)
    base.update(kw)
    return base


class T(unittest.TestCase):
    def test_obsolete(self):
        self.assertTrue(ht.is_obsolete(item(label="auto-author-rt-egift-link-s1-s4")))
        self.assertTrue(ht.is_obsolete(item(cwd="/x/wt-rt-bfmr-tls-fingerprint")))
        self.assertFalse(ht.is_obsolete(item(label="rt-egift-other")))

    def test_hard_exclusions(self):
        self.assertIsNone(ht.hard_exclusion(item(), None, set()))
        for kw in (dict(awaiting_signoff=True), dict(code_high=1), dict(untrusted=True),
                   dict(status="failed"), dict(gate="FAIL"), dict(gate="concerns (x)"),
                   dict(gate="fail"), dict(not_checked=2), dict(status="needs_opus")):
            self.assertIsNotNone(ht.hard_exclusion(item(**kw), None, set()), kw)
        self.assertIsNotNone(ht.hard_exclusion(item(), "b1", {"b1"}))
        self.assertIsNone(ht.hard_exclusion(item(), "b1", {"b2"}))

    def test_grounding(self):
        ok = ht.grounded({"class": "routine-done", "evidence_quote": "- **status**: `done`  (exit 0)"}, PAGE)
        self.assertTrue(ok["grounded"]); self.assertEqual(ok["class"], "routine-done")
        bad = ht.grounded({"class": "routine-done", "evidence_quote": "status: done, all good"}, PAGE)
        self.assertEqual(bad["class"], "needs-action")
        multi = ht.grounded({"class": "landed", "evidence_quote": "`done`  (exit 0)\nline two"}, PAGE)
        self.assertEqual(multi["class"], "needs-action")
        self.assertEqual(ht.grounded({"class": "bogus", "evidence_quote": "x" * 20}, PAGE)["class"], "needs-action")
        self.assertEqual(ht.grounded(None, PAGE)["class"], "needs-action")

    def test_parse_json(self):
        self.assertEqual(ht.parse_json('noise {"a": 1} tail'), {"a": 1})
        self.assertIsNone(ht.parse_json("no json"))

    def test_clear_reason_has_no_merge_words(self):
        # reasons for non-obsolete clears must not trip handoff-emit's merge-claim gate
        r = "triage: gemma-4-26b classified routine, gate PASS (routine-done)"
        self.assertFalse(any(w in r.lower() for w in ht.MERGE_WORDS))

    def test_landed_fraction_unknown_without_repo(self):
        self.assertIsNone(ht.landed_fraction("```diff\n+x\n```", "/nonexistent"))


if __name__ == "__main__":
    unittest.main()
