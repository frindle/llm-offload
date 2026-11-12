#!/usr/bin/env python3
"""test-sampling-ladder.py -- the sampling ESCALATION LADDER (A/B arm, DEFAULT OFF; 2026-10-09).

  1  model_profiles.yaml declares the arm, shipped OFF, evidence cited and graded; check_profiles clean
  2  model_profile.build_request_fields: off => byte-identical body; on => step 1 / step 2 overrides
     (temp>=0.8 + rep 1.2 + seed / presence 1.0 + thinking off ONLY under the hybrid thinking arm),
     roles outside `roles:` untouched, check_sampling rejects a shipped arm != off
  3  worker_robust.SamplingLadder: abort1 -> step1, abort2 -> step2, abort3 -> sampling_ladder_exhausted;
     two aborts on the SAME failing check -> spec_defect_repeat_abort (spec path first, no more sampling);
     fresh seed on every take; one-shot; inert when disabled; reasons registered as terminal exit reasons
  4  worker wiring (source-level): ladder state, per-turn take, abort hooks, verify-signature feed, on_ok
Usage: test-sampling-ladder.py [--bin DIR]. Prints SAMPLING_LADDER_TEST_OK.
"""
import argparse, os, sys, tempfile
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--bin", default=str(Path(__file__).resolve().parent))
BIN = Path(ap.parse_args().bin)
os.environ["HOME"] = tempfile.mkdtemp(prefix="samplad-home-")
os.environ.pop("MODEL_SAMPLING_ARM", None)
os.environ.pop("MODEL_THINKING_ARM", None)
os.environ.setdefault("MODEL_PROFILES_PATH", str(BIN / "model_profiles.yaml"))
sys.path.insert(0, str(BIN))
R = []


def check(name, got, want):
    ok = got == want
    R.append(ok)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"\n     got={got!r}\n    want={want!r}"))


import yaml
import model_profile as mp
import worker_robust as wr

RAW = yaml.safe_load((BIN / "model_profiles.yaml").read_text())
M = "qwen3.6-35b-a3b-vl-mtp-mxfp8"

# ---- 1 yaml
cfg = RAW["sampling_escalation"]
check("1 shipped arm is off", cfg["arm"] in ("off", False), True)
check("1 evidence cites the cards and says unvalidated on Qwen3.6",
      all(t in cfg["evidence"] for t in ("L663-665", "L327-329", "unvalidated on Qwen3.6")), True)
check("1 check_sampling on the shipped yaml is clean", mp.check_sampling(RAW), [])
check("1 check_profiles reports no sampling_escalation problem",
      [x for x in mp.check_profiles() if "sampling_escalation" in x], [])
check("1 check_sampling rejects a shipped arm=ladder",
      bool(mp.check_sampling({"sampling_escalation": {"arm": "ladder", "evidence": "x"}, "roles": {}})), True)
check("1 check_sampling rejects a missing evidence note",
      any("evidence" in p for p in mp.check_sampling({"sampling_escalation": {"arm": "off"}, "roles": {}})), True)
check("1 check_sampling rejects an out-of-range value",
      any("outside" in p for p in mp.check_sampling(
          {"sampling_escalation": {"arm": "off", "evidence": "x", "ladder": {"1": {"temperature_min": 9}}}, "roles": {}})), True)
check("1 check_sampling rejects a step-3 entry (the exit is not a sampling step)",
      any("unknown step" in p for p in mp.check_sampling(
          {"sampling_escalation": {"arm": "off", "evidence": "x", "ladder": {"3": {}}}, "roles": {}})), True)

# ---- 2 builder
plain = mp.build_request_fields(M, "author", "openai")
check("2 arm off: step + seed change nothing", mp.build_request_fields(M, "author", "openai", sampling_step=1, seed=5), plain)
check("2 arm off: no seed key", "seed" in mp.build_request_fields(M, "author", "openai", sampling_step=2, seed=5), False)
s1 = mp.build_request_fields(M, "author", "openai", sampling_step=1, seed=5, sampling_arm="ladder")
check("2 step 1: temp>=0.8, rep 1.2, seed", (s1["temperature"], s1["repetition_penalty"], s1["repeat_penalty"], s1["seed"]), (0.8, 1.2, 1.2, 5))
check("2 step 1: thinking untouched", s1["chat_template_kwargs"], plain["chat_template_kwargs"])
s2 = mp.build_request_fields(M, "author", "openai", sampling_step=2, seed=6, sampling_arm="ladder")
check("2 step 2 without the hybrid thinking arm: presence 1.0, thinking NOT turned off",
      (s2["presence_penalty"], s2["chat_template_kwargs"]["enable_thinking"]), (1.0, True))
s2h = mp.build_request_fields(M, "author", "openai", sampling_step=2, seed=6, sampling_arm="ladder", arm="hybrid")
check("2 step 2 under hybrid: presence 1.0, thinking off, seed", (s2h["presence_penalty"], s2h["chat_template_kwargs"], s2h["seed"]),
      (1.0, {"enable_thinking": False}, 6))
s2o = mp.build_request_fields(M, "author", "ollama", sampling_step=2, seed=6, sampling_arm="ladder", arm="hybrid")
check("2 step 2 native ollama: options.presence_penalty + options.seed, think False",
      (s2o["options"]["presence_penalty"], s2o["options"]["seed"], s2o["think"]), (1.0, 6, False))
check("2 a role outside `roles:` is untouched", mp.build_request_fields(M, "review", "openai", sampling_step=2, seed=1, sampling_arm="ladder"),
      mp.build_request_fields(M, "review", "openai"))
check("2 explicit think=True beats the ladder's thinking-off",
      mp.build_request_fields(M, "author", "openai", sampling_step=2, seed=1, sampling_arm="ladder", arm="hybrid", think=True)
      ["chat_template_kwargs"]["enable_thinking"], True)
check("2 a higher caller temperature is not lowered (max, never min)",
      mp.build_request_fields(M, "author", "openai", overrides={"temperature": 0.95}, sampling_step=1, seed=1, sampling_arm="ladder")["temperature"], 0.95)
os.environ["MODEL_SAMPLING_ARM"] = "ladder"
check("2 env MODEL_SAMPLING_ARM=ladder enables the arm", mp.sampling_ladder_enabled("author"), True)
check("2 env arm: unknown arm name falls back to off",
      (os.environ.__setitem__("MODEL_SAMPLING_ARM", "bogus"), mp.sampling_ladder_enabled("author"))[1], False)
os.environ.pop("MODEL_SAMPLING_ARM")
check("2 default: ladder not enabled", mp.sampling_ladder_enabled("author"), False)

# ---- 3 state machine
L = wr.SamplingLadder(True, 7)
check("3 abort 1 -> step 1", L.on_abort("cutoff"), ("step", 1))
st, sd = L.take()
check("3 take returns step 1 with a seed, one-shot", (st, isinstance(sd, int), L.take()), (1, True, (0, None)))
check("3 abort 2 (no known check) -> step 2", L.on_abort("cutoff"), ("step", 2))
st2, sd2 = L.take()
check("3 a fresh seed per take", (st2, sd2 != sd), (2, True))
check("3 abort 3 -> sampling_ladder_exhausted", L.on_abort("cutoff"), ("exit", "sampling_ladder_exhausted"))
check("3 exit leaves nothing pending", L.take(), (0, None))
L = wr.SamplingLadder(True, 7)
L.note_check({"E1"})
L.on_abort("cutoff")
L.on_ok()
check("3 two aborts on the SAME failing check (a good turn between) -> spec_defect_repeat_abort",
      L.on_abort("runaway"), ("exit", "spec_defect_repeat_abort"))
L = wr.SamplingLadder(True, 7)
L.note_check({"E1"})
L.on_abort("cutoff")
L.note_check({"E2"})
check("3 a DIFFERENT failing check keeps climbing the ladder", L.on_abort("cutoff"), ("step", 2))
L = wr.SamplingLadder(True, 7)
L.on_abort("cutoff")
L.on_ok()
check("3 a good turn resets the consecutive count (back to step 1)", L.on_abort("cutoff"), ("step", 1))
L = wr.SamplingLadder(False, 7)
check("3 disabled: inert", (L.on_abort("cutoff"), L.take()), (("off", None), (0, None)))
check("3 round-specific seed base gives a different seed",
      wr.SamplingLadder(True, 1).take() == (0, None) and
      (lambda a, b: (a.on_abort(), b.on_abort(), a.take()[1] != b.take()[1])[2])(wr.SamplingLadder(True, 1), wr.SamplingLadder(True, 2)), True)
check("3 both exit reasons are registered terminal reasons",
      {"spec_defect_repeat_abort", "sampling_ladder_exhausted"} <= set(wr.NEW_EXIT_REASONS), True)

# ---- 4 worker wiring
W = (BIN / "ollama-worker.py").read_text()
for name, frag in (
        ("ladder enabled from the profile arm", "enabled=_mp.sampling_ladder_enabled(role)"),
        ("per-job Sampling-arm task line sets the arm", 'os.environ["MODEL_SAMPLING_ARM"] = _sa_m.group(1).lower()'),
        ("per-turn take at the turn top AND on the runaway retry", "#2:_turn_sstep, _turn_seed = _ladder.take()"),
        ("cut-off abort hook", '_lad = _ladder.on_abort("cutoff")'),
        ("reasoning-runaway abort hook", '_lad = _ladder.on_abort("reasoning_runaway")'),
        ("good turn resets", "_ladder.on_ok()"),
        ("verify signature fed at both verify sites", None),
        ("step reaches the request builder", "think=think, sampling_step=sampling_step, seed=seed, reasoning_parser=_rparser)"),
        ("step reaches the streaming call", "sampling_step=_turn_sstep, seed=_turn_seed)")):
    if frag is None:
        ok4 = W.count("_ladder.note_check(_verify_failure_signature(v_out)") == 2
    elif frag.startswith("#2:"):
        ok4 = W.count(frag[3:]) == 2
    else:
        ok4 = frag in W
    check("4 worker: " + name, ok4, True)
check("4 worker: all four senders accept sampling_step/seed",
      W.count("sampling_step: int = 0, seed: int = None") >= 3 and "sampling_step=0, seed=None, host=None):" in W, True)
print()
print("%d/%d checks" % (sum(R), len(R)))
if not all(R):
    sys.exit(1)
print("SAMPLING_LADDER_TEST_OK")
