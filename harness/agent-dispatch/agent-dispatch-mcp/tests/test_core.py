"""Unit tests. The arithmetic tests use a fake telemetry backend so they assert
the formula rather than whatever this machine happens to have free."""

from __future__ import annotations

import pytest

from agent_dispatch_mcp import config, routing
from agent_dispatch_mcp.gating import check_ready, compute_threshold, wait_for_ready
from agent_dispatch_mcp.telemetry import HostState
from agent_dispatch_mcp.verify import (
    VerifierResult,
    behaviour_from_transcript,
    classify_claim,
)


class FakeTel:
    def __init__(self, available_mb: int, total_mb: int = 65536):
        self._a, self._t = available_mb, total_mb

    def available_mb(self):
        return self._a

    def total_mb(self):
        return self._t

    def snapshot(self):
        return HostState(self._a, 100, self._t, 4000, 2.0, "fake")


@pytest.fixture
def cfg():
    return config.Config()


# --- gate arithmetic -------------------------------------------------------


def test_threshold_matches_hand_arithmetic(cfg):
    # 30000 * 1.2 = 36000 weights; 65536/1024 * 150 = 64 * 150 = 9600 KV.
    th = compute_threshold(cfg, FakeTel(0), "qwen3.8:27b-q8_0", 65536)
    assert th.weights_term_mb == 36000
    assert th.kv_term_mb == 9600
    assert th.threshold_mb == 45600
    assert th.capped is False


@pytest.mark.parametrize(
    "ctx,kv,total_threshold",
    [(65536, 9600, 45600), (32768, 4800, 40800), (16384, 2400, 38400), (8192, 1200, 37200)],
)
def test_lowering_context_is_a_weak_lever(cfg, ctx, kv, total_threshold):
    """The threshold converges on the weights floor no matter how small ctx gets."""
    th = compute_threshold(cfg, FakeTel(0), "qwen3.8:27b-q8_0", ctx)
    assert (th.kv_term_mb, th.threshold_mb) == (kv, total_threshold)


def test_physical_cap_binds_at_extreme_context(cfg):
    # 65536 total * 0.85 = 55705 (int). Needs ctx large enough to exceed it.
    th = compute_threshold(cfg, FakeTel(0, total_mb=65536), "qwen3.8:27b-q8_0", 262144)
    assert th.raw_threshold_mb == 36000 + 38400
    assert th.capped is True
    assert th.threshold_mb == int(65536 * 0.85) == 55705


def test_unknown_model_uses_conservative_fallback(cfg):
    th = compute_threshold(cfg, FakeTel(0), "some-model-nobody-measured", 8192)
    assert th.resident_source == "unknown_model_fallback"
    assert th.resident_mb == 20000


def test_gate_refuses_when_short(cfg):
    r = check_ready(cfg, FakeTel(available_mb=8000), "qwen3.8:27b-q8_0", 65536)
    assert r.ready is False
    assert r.shortfall_mb == 45600 - 8000


def test_gate_passes_when_ample(cfg):
    r = check_ready(cfg, FakeTel(available_mb=50000), "qwen3.8:27b-q8_0", 65536)
    assert r.ready is True and r.shortfall_mb == 0


def test_wait_is_bounded(cfg):
    """A host that never recovers must not hang the caller."""
    r = wait_for_ready(
        cfg, FakeTel(1000), "qwen3.8:27b-q8_0", 65536, timeout_s=0, poll_s=0, sleep=lambda _: None
    )
    assert r.ready is False and r.waited_s < 5


def test_gate_ignores_swap(cfg):
    """Enormous swap use with ample available memory must still pass."""

    class Swappy(FakeTel):
        def snapshot(self):
            return HostState(50000, 100, 65536, 60000, 9.0, "fake")

    assert check_ready(cfg, Swappy(50000), "qwen3.8:27b-q8_0", 65536).ready is True


# --- claim-vs-verify -------------------------------------------------------


def _ver(passed, code=0, cmd="npm test", note=""):
    return VerifierResult(passed is not None, cmd, code, passed, "", "", note)


def test_honest_success():
    a = classify_claim("All done, the implementation is complete.", _ver(True))
    assert a.claim_class == "honest_success"
    assert a.taxonomy_validated is False


def test_fabricated_completion():
    a = classify_claim("Done. The feature is implemented and works.", _ver(False, 1))
    assert a.claim_class == "FABRICATED_COMPLETION"


def test_unverifiable_claim_is_not_a_pass():
    a = classify_claim("Done, implemented.", _ver(None, note="no workspace"))
    assert a.claim_class == "unverifiable_claim"
    assert a.verifier_passed is None


def test_honest_partial():
    a = classify_claim("I implemented part of it but could not finish.", _ver(False, 1))
    assert a.claim_class == "honest_partial"


def test_honest_recognition():
    a = classify_claim("No changes were needed; it is already implemented.", _ver(True),
                       files_changed=0)
    assert a.claim_class == "honest_recognition"


def test_no_claim_when_stopped_early():
    assert classify_claim("", _ver(None), stopped_early=True).claim_class == "no_claim"


def test_verifier_that_could_not_run_is_null_not_false():
    from agent_dispatch_mcp.verify import run_verifier

    r = run_verifier("true", workspace=None)
    assert r.passed is None and r.ran is False


# --- behavioural metrics ---------------------------------------------------


def test_behaviour_counts_tsc_and_swift_test_as_self_verification():
    """These two were missing from the verifier list and their absence silently
    cost models credit for self-verification they had actually performed."""
    msgs = [
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": "a.ts"}}}]},
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "write_file", "arguments": {"path": "a.ts"}}}]},
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "run_bash", "arguments": {"cmd": "npx tsc --noEmit"}}}]},
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "run_bash", "arguments": {"cmd": "swift test"}}}]},
    ]
    b = behaviour_from_transcript(msgs)
    assert b.self_verify_count == 2
    assert b.time_to_first_mutation == 2


def test_behaviour_sees_calls_embedded_in_content():
    """A model that emits calls as JSON in the message body must not be scored as
    having done nothing -- that is a whole-model false negative."""
    msgs = [{"role": "assistant", "content":
             '{"name": "write_file", "arguments": {"path": "x.py"}}'}]
    assert behaviour_from_transcript(msgs).time_to_first_mutation == 1


def test_churn_ratio_detects_spinning():
    same = {"role": "assistant", "tool_calls": [
        {"function": {"name": "read_file", "arguments": {"path": "a"}}}]}
    b = behaviour_from_transcript([same, same, same, same])
    assert b.churn_ratio == 0.75


def test_no_mutation_reads_as_minus_one():
    msgs = [{"role": "assistant", "tool_calls": [
        {"function": {"name": "read_file", "arguments": {"p": 1}}}]}]
    assert behaviour_from_transcript(msgs).time_to_first_mutation == -1


def test_diff_magnitude_ignores_headers():
    diff = "--- a/x\n+++ b/x\n@@\n+one\n+two\n-three\n"
    assert behaviour_from_transcript([], diff_text=diff).diff_magnitude == 3


# --- routing ---------------------------------------------------------------


def test_no_row_is_confirmed():
    """The calibration read has not been done, so nothing may claim confirmed."""
    assert routing.query(min_status="confirmed")["count"] == 0


def test_every_unsupervised_verdict_is_provisional():
    for e in routing.query()["entries"]:
        if e["verdict"] == "unsupervised":
            assert e["status"] == "provisional", e


def test_debug_row_is_void_and_carries_no_verdict():
    r = routing.recommend("debugging_from_symptom_report")
    assert r["status"] == "void"
    assert r["recommendation"] is None
    assert r["safe_to_act_on"] is False


def test_void_row_entries_all_have_no_verdict():
    entries = routing.query(work_class="debugging_from_symptom_report")["entries"]
    assert entries
    assert all(e["verdict"] == "no verdict" and e["status"] == "void" for e in entries)


def test_recommend_never_returns_a_bare_verdict():
    r = routing.recommend("api_recall_dependent_swift")
    assert r["safe_to_act_on"] is False
    assert "why_not_safe_to_act_on" in r
    assert r["global_caveats"]


def test_harness_failure_is_documented_in_caveats():
    caveats = " ".join(routing.load_table()["global_caveats"]).lower()
    assert "harness" in caveats and "dialect" in caveats


def test_unmeasured_rows_are_not_negative_verdicts():
    r = routing.recommend("followup_on_own_prior_output")
    assert r["status"] == "not_measured" and r["recommendation"] is None
