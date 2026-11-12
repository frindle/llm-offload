#!/usr/bin/env python3
"""Revert-test: diff-attributed VERIFY FAILED feedback + stop-early guard (ollama-worker.py).

Live 2026-10-02, f21621ab528f (sidecar-bfmr-sink-s1-route): the diff introduced
TS18047 at route.ts:287. The model was fed the raw verify output under a note that
the verify "was ALREADY FAILING before you started", called the error "pre-existing in
the original code", and the run was accepted as a final answer at 12/30 after two
silent-stop nudges (turns 10-12 were also each cut off at exactly 8192 tokens).

Drives the REAL run_task with the model stubbed (no GPU/network), replaying that shape:
  turn 1  write_file introduces the new diagnostic
  turns   silent prose "this is pre-existing" (no tool call) -- several times, one of
          them cut off at the output cap
  then    write_file fixes it, task_complete
Asserts: the run is NOT ended by the silent stops (only by the fix / max_iters); the
feedback lists ONLY the new diagnostic as file:line: message, says the diff introduced
it, and never uses "pre-exist" / "ALREADY FAILING" wording next to it; a prose turn cut
off at the cap is answered with the CUT OFF nudge; with no fix the run lasts to
max_iters. Plus the pure helpers.

Run: python3 test-worker-verify-feedback.py [--revert-check]
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

WORKER = Path(os.environ.get("WORKER_SRC") or Path(__file__).resolve().parent / "ollama-worker.py")
FAILS = []


def ok(name, cond, detail=""):
    print(f"  ok   {name}" if cond else f"  FAIL {name} {detail}")
    if not cond:
        FAILS.append(name)


def _load():
    os.environ["HOME"] = tempfile.mkdtemp(prefix="vfb-home-")
    os.environ.pop("VAULT_TOKEN_FILE", None)
    spec = importlib.util.spec_from_file_location("ollama_worker_vfb", WORKER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.log_dispatch_to_vault = lambda *a, **k: None
    assert str(m.LOG_DIR).startswith(os.environ["HOME"]), m.LOG_DIR
    return m


VERIFY_SH = """#!/bin/bash
rc=0
grep -q OLD old.ts && { echo "old.ts(1,1): error TS1000: baseline thing."; rc=1; }
grep -q BUG route.ts && { echo "route.ts(287,11): error TS18047: 'emailSetting' is possibly 'null'."; rc=1; }
exit $rc
"""


def tc(name, args):
    return {"message": {"role": "assistant", "content": "",
                        "tool_calls": [{"function": {"name": name, "arguments": args}}]},
            "done_reason": "stop", "usage": {"prompt_tokens": 1000, "completion_tokens": 40}}


def prose(text, cut=False, cap=8192):
    return {"message": {"role": "assistant", "content": text},
            "done_reason": "length" if cut else "stop",
            "usage": {"prompt_tokens": 1000, "completion_tokens": cap if cut else 60}}


def drive(m, script, max_iters):
    td = tempfile.mkdtemp(prefix="vfb-wt-")
    wt = Path(td)
    subprocess.run(["git", "init", "-q", "."], cwd=wt, check=True)
    (wt / "old.ts").write_text("OLD\n")
    (wt / "route.ts").write_text("ok\n")
    (wt / "verify.sh").write_text(VERIFY_SH)
    subprocess.run(["git", "add", "."], cwd=wt, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b"],
                   cwd=wt, check=True)
    sent = []
    seq = list(script)

    def fake_call(host, model, messages, temperature, num_ctx, **kw):
        sent.append(json.loads(json.dumps(messages)))
        return seq.pop(0) if seq else prose("this is pre-existing, nothing more to do")

    m.call_ollama = fake_call
    m.call_ollama_streaming = lambda *a, **k: fake_call(*a, **k)
    m.ensure_model_ready = lambda *a, **k: None
    m.set_keep_alive = lambda *a, **k: None
    m.clamp_unraid_ctx = lambda host, model, n: n
    logs = []
    orig = m.log
    m.log = lambda s, *a, **k: logs.append(str(s))
    try:
        m.run_task("stub-model", "http://127.0.0.1:9", str(wt), "fix route.ts", "bash verify.sh",
                   max_iters, 0.0, 65536, None, max_tokens=8192, verify_failed_at_baseline=True)
    except SystemExit:
        pass
    except Exception as e:
        logs.append(f"run_task raised: {e!r}")
    finally:
        m.log = orig
    return sent, logs


def user_msgs(sent):
    return [x["content"] for x in (sent[-1] if sent else []) if x.get("role") == "user"]


def main():
    m = _load()
    print("== pure helpers ==")
    ok("tsc line -> file:line: message",
       m.format_new_diagnostic("app/r.ts(287,11): error TS18047: 'a' is possibly 'null'.")
       == "app/r.ts:287: error TS18047: 'a' is possibly 'null'.")
    ok("gcc-style line kept as file:line: message",
       m.format_new_diagnostic("x.py:12:3: E999 bad") == "x.py:12: E999 bad")
    fb = m.new_failure_feedback(["r.ts(287,11): error TS18047: x"])
    ok("feedback says the diff introduced it", "INTRODUCED" in fb and "did NOT exist" in fb)
    ok("feedback never says pre-existing", "pre-exist" not in fb.lower())
    ok("no baseline captured -> nothing attributable", m.attributable_new_failures(["a"], None) == [])
    ok("passing baseline (empty set) -> all attributable", m.attributable_new_failures(["a"], set()) == ["a"])
    ok("new failures never accepted past the nudge cap",
       m.silent_stop_decision(False, ["a"], 99, 2, False, False, True) == "nudge")
    ok("no new failures, cap spent -> accept",
       m.silent_stop_decision(False, [], 2, 2, True, False, True) == "accept")
    ok("verify passes -> accept", m.silent_stop_decision(True, [], 0, 2, True, False, False) == "accept")
    ok("cut-off prose detected",
       m.output_cap_cut_prose({"content": "x"}, {"completion_tokens": 8192}, None, 8192))
    ok("normal prose stop not a cut",
       not m.output_cap_cut_prose({"content": "x"}, {"completion_tokens": 60}, "stop", 8192))

    print("== e2e: f21621 replay, then the model fixes it ==")
    script = [tc("write_file", {"path": "route.ts", "content": "BUG\n"}),
              prose("The tsc error on line 287 is a pre-existing error in the original code."),
              prose("Still pre-existing. " * 50, cut=True),
              prose("It is pre-existing, I am done."),
              prose("Pre-existing. Done."),
              tc("write_file", {"path": "route.ts", "content": "fixed\n"}),
              tc("write_file", {"path": "old.ts", "content": "new\n"}),
              tc("task_complete", {"summary": "fixed"})]
    sent, logs = drive(m, script, 12)
    ok("silent stops did NOT end the run: the fix turns were reached", len(sent) >= 8,
       f"calls={len(sent)} tail={logs[-4:]}")
    um = user_msgs(sent)
    fbs = [u for u in um if "INTRODUCED" in u]
    ok("diff-introduced failure fed back", bool(fbs), str(um[-3:])[:400])
    if fbs:
        head = fbs[0].split("Fix those first")[0]
        ok("listed as file:line: message", "route.ts:287: error TS18047" in head, head[:300])
        ok("list holds ONLY the new diagnostic (baseline old.ts absent)", "old.ts" not in head, head[:300])
        ok("no pre-existing / ALREADY FAILING wording in that feedback",
           "pre-exist" not in fbs[0].lower() and "already failing" not in fbs[0].lower(), fbs[0][:400])
    ok("cut-off prose turn answered with the CUT OFF nudge", any("CUT OFF" in u for u in um))
    ok("never accepted as a final answer while the new failure stood",
       not any("treating as final answer" in l for l in logs[:len(logs)]) or len(sent) >= 8)

    print("== e2e: model never fixes it -> runs to max_iters ==")
    sent, logs = drive(m, [tc("write_file", {"path": "route.ts", "content": "BUG\n"})], 6)
    ok("silent stops cannot end the run early: model called max_iters times", len(sent) == 6,
       f"calls={len(sent)} tail={logs[-3:]}")

    print()
    if FAILS:
        print(f"VERIFY_FEEDBACK_FAILED: {len(FAILS)}: {FAILS}")
        return 1
    print("VERIFY_FEEDBACK_OK")
    return 0


MUTATIONS = [
    ("silent stop accepted past nudge cap",
     "    if attributable_new:\n        return \"nudge\"\n    if nudges >= max_nudges:",
     "    if nudges >= max_nudges:\n        return \"accept\"\n    if attributable_new:"),
    ("old baseline wording returns",
     "            _fb = new_failure_feedback(_diff_new, v_out, _baseline_is_the_task)",
     "            _fb = \"the verify still fails:\\n\" + v_out[-3000:] + \"\\n(This verify was ALREADY FAILING before you started; pre-existing failures.)\""),
    ("cut-off prose read as final answer",
     "        if task_kind == \"coding\" and output_cap_cut_prose(",
     "        if False and output_cap_cut_prose("),
    ("raw diagnostic lines (no file:line)",
     "            return \"%s:%s: %s\" % (m.group(\"f\"), m.group(\"l\"), m.group(\"m\").strip())",
     "            return t"),
]


def revert_check():
    bad = 0
    src = WORKER.read_text()
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-mut.py", delete=False) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "WORKER_SRC": f.name},
                           capture_output=True, text=True, timeout=600)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
