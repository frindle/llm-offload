#!/usr/bin/env python3
"""needs_info slices R1-R10 for mini-swe-runner.py (no model, no GPU, no queue): python3 ~/bin/test_mini_needsinfo_runner.py
Prints PASS/FAIL lines; exit 1 on any FAIL."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNNER = HERE / "mini-swe-runner.py"
spec_ = importlib.util.spec_from_file_location("mini_swe_runner_ni", RUNNER)
R = importlib.util.module_from_spec(spec_)
spec_.loader.exec_module(R)
FAILS = []


def check(name, got, want=True):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f" (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def setenv(**kw):
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


with tempfile.TemporaryDirectory() as D:
    D = Path(D)
    # R1 mode flag
    flag = D / "flag"
    setenv(MINI_NEEDS_INFO=None, MINI_NEEDS_INFO_FILE=str(flag))
    check("R1 unset + no file -> off", R.needs_info_mode(), "off")
    for env, want in (("on", "on"), ("shadow", "shadow"), ("ON ", "on"), ("bogus", "off"), ("", "off"), ("off", "off")):
        setenv(MINI_NEEDS_INFO=env)
        check(f"R1 env {env!r} -> {want}", R.needs_info_mode(), want)
    setenv(MINI_NEEDS_INFO=None)
    flag.write_text("shadow\n")
    check("R1 flag file shadow", R.needs_info_mode(), "shadow")
    flag.write_text("junk")
    check("R1 flag file junk -> off", R.needs_info_mode(), "off")
    flag.write_text("on")
    setenv(MINI_NEEDS_INFO="off")
    check("R1 env off beats flag file on", R.needs_info_mode(), "off")

    # R2 prompt
    spec = {"test_file": "t.js", "test_cmd": "node t.js", "task": str(D / "TASK.md")}
    setenv(MINI_NEEDS_INFO="off")
    p_off = R.phase_b_prompt(spec, ["a.js"], "T")
    setenv(MINI_NEEDS_INFO="on")
    p_on = R.phase_b_prompt(spec, ["a.js"], "T")
    check("R2 off prompt has no needs-info text", "NEEDS" not in p_off)
    check("R2 on prompt == off prompt + rule block", p_on.replace(R.NEEDS_RULE, "") == p_off and R.NEEDS_RULE in p_on)
    check("R2 steps chain step never offered", "NEEDS.md" not in R.phase_b_prompt({**spec, "_guard": {}}, ["a.js"], "T"))
    check("R2 no job-local task -> not offered", "NEEDS.md" not in R.phase_b_prompt({**spec, "task": "TASK.md"}, ["a.js"], "T"))

    # R4 ask count
    ans = Path(str(D / "TASK.md") + ".answers.md")
    check("R4 missing sidecar -> 0", R.ask_count(spec), 0)
    ans.write_text("### Q1: x?\nyes\n<!-- test-sha: abc123 -->\n")
    check("R4 one entry -> 1", R.ask_count(spec), 1)
    ans.write_text("### Q1: x?\nyes\n### Q2: y?\nno\n<!-- test-sha: abc123 -->\n")
    check("R4 two entries -> 2", R.ask_count(spec), 2)
    check("R4 recorded test sha", R.answers_test_sha(spec), "abc123")
    ans.unlink()

    # R3 gate
    repo = D / "repo"
    repo.mkdir()
    (repo / "config.py").write_text("RATE_LIMIT = 5\n")
    (repo / "other.py").write_text("x = 1\n")
    subprocess.run("git init -q && git add -A && git -c user.email=t@t -c user.name=t commit -qm b", shell=True, cwd=repo, check=True)
    steps = [("grep -rn 'quota' .", "nothing"), ("cat other.py", "x = 1"), ("rg -n limit src", "")]
    GOOD = ("## Question\nWhat is the vendor's real daily quota for the billing API?\n\n## Tried\n`grep -rn 'quota' .` found nothing; `cat other.py` no hint.\n\n"
            "## If answer is A\nSet the limit to 100.\n\n## If answer is B\nSet the limit to 1000.\n")
    ok, why = R.check_needs(repo, steps, GOOD, 0, 1)
    check("R3 valid ask accepted", (ok, why), (True, ""))
    for name, text, st, n, frag in (
            ("missing heading", GOOD.replace("## If answer is B", "## Else"), steps, 0, "missing heading"),
            ("empty question", GOOD.replace("What is the vendor's real daily quota for the billing API?", ""), steps, 0, "Question is empty"),
            ("long question", GOOD.replace("What is", "x" * 700 + " What is"), steps, 0, "max 600"),
            ("no searches", GOOD, [("echo hi", "hi")], 0, "fewer than 2"),
            ("fabricated Tried", GOOD.replace("grep -rn 'quota' .", "grep -rn zzz /nowhere").replace("cat other.py", "cat nothing.txt"), steps, 0, "no command that appears"),
            ("names unread repo file", GOOD.replace("the vendor's real daily quota", "the value in `config.py`"), steps, 0, "never read"),
            ("asks to change the test", GOOD.replace("What is the vendor's real daily quota for the billing API?", "May I remove the failing assert in the test?"), steps, 0, "test"),
            ("cap reached", GOOD, steps, 1, "ask cap")):
        ok, why = R.check_needs(repo, st, text, n, 1)
        check(f"R3 {name} rejected", (not ok) and frag in why, True)
        if ok or frag not in why:
            print("   why:", why)
    read_steps = steps + [("cat config.py", "RATE_LIMIT = 5")]
    ok, why = R.check_needs(repo, read_steps, GOOD.replace("the vendor's real daily quota", "the value in `config.py`").replace("`grep -rn 'quota' .`", "`cat config.py`"), 0, 1)
    check("R3 naming a repo file that WAS read is fine", ok, True)

    # e2e through the real runner with a stub model
    TOY = R._toy
    stub = D / "mini"
    stub.write_text(R.STUB_MINI)
    stub.chmod(0o755)
    (D / "local.json").write_text(json.dumps({"base_url": "http://127.0.0.1:9", "api_key": "k"}))

    def run(name, plan, mode, pre=None, spec_over=None, shadow_log=None):
        d = D / name
        d.mkdir()
        base = TOY(d)
        (base / "calc.test.js").write_text(R.GOOD_T)  # a supplied (frozen) test
        (d / "work").mkdir()
        (d / "TASK.md").write_text((base / "TASK.md").read_text())
        sp = {"mode": "two-phase", "allowed": ["calc.js"], "baseline_from": str(base), "test_file": "calc.test.js", "test_cmd": "node --test calc.test.js",
              "target_fn": "add", "timeout": 600, "fix_attempts": 2, "task": str(d / "TASK.md")}
        sp.update(spec_over or {})
        (d / "spec.json").write_text(json.dumps(sp))
        (d / "plan.json").write_text(json.dumps(plan))
        if pre:
            pre(d)
        env = {**os.environ, "MINI_BIN": str(stub), "DARKBLOOM_LOCAL_JSON": str(D / "local.json"), "STUB_PLAN": str(d / "plan.json"),
               "MINI_NEEDS_INFO_FILE": str(D / "none"), "MINI_NEEDS_INFO_SHADOW_LOG": str(shadow), "HOME": os.environ["HOME"]}
        env.pop("MINI_NEEDS_INFO", None)
        if mode:
            env["MINI_NEEDS_INFO"] = mode
        r = subprocess.run([sys.executable, str(RUNNER), "--cwd", str(d / "work"), "--task-file", str(d / "spec.json")], capture_output=True, text=True, env=env)
        return d, r

    NEEDS = ("## Question\nWhich rounding mode does the vendor use for add?\n\n## Tried\n`grep -rn round .` found nothing; `cat calc.js` shows add only.\n\n"
             "## If answer is A\nround half up.\n\n## If answer is B\nround half even.\n")
    ASK = {"A": {}, "B": {}, "needs": NEEDS, "cmds": ["grep -rn round .", "cat calc.js"]}
    FIX = {"A": {}, "B": {"calc.js": R.CALC_FIXED}}
    shadow = D / "shadow.jsonl"

    # R5/R6 park
    d, r = run("park", ASK, "on")
    marker = d / "work.needs-info.json"
    check("R6 on + valid NEEDS -> exit 3", r.returncode, 3)
    check("R6 RESUMABLE TRANSCRIPT line names the marker", f"[worker] RESUMABLE TRANSCRIPT: {marker}" in r.stdout, True)
    m = json.loads(marker.read_text()) if marker.exists() else {}
    check("R6 marker pause_reason needs_info", m.get("pause_reason"), "needs_info")
    pm = m.get("pause_meta", {})
    check("R6 marker meta carries question/ask_n/test_sha/task/answers",
          bool(pm.get("question")) and pm.get("ask_n") == 1 and len(pm.get("test_sha", "")) == 64 and pm.get("task") == str(d / "TASK.md")
          and pm.get("answers") == str(d / "TASK.md") + ".answers.md", True)
    check("R6 no done RESULT PASS/FAIL line", "RESULT PAUSED needs-info" in r.stdout and "RESULT PASS" not in r.stdout and "RESULT FAIL" not in r.stdout, True)
    check("R6 NEEDS.md removed from the work dir", not (d / "work" / "NEEDS.md").exists(), True)
    st = subprocess.run(["git", "status", "--short", "--untracked-files=all"], cwd=d / "work", capture_output=True, text=True).stdout
    check("R6 NEEDS.md absent from the working-tree status", "NEEDS.md" not in st, True)

    # R6 on + invalid ask -> not parked (rejected, counted)
    bad = {**ASK, "cmds": ["echo hi"]}
    d, r = run("rejected", bad, "on", spec_over={"fix_attempts": 1})
    check("R6 on + invalid NEEDS -> no pause", r.returncode == 1 and "NEEDS-INFO rejected" in r.stdout, True)

    # R7 shadow / off
    d, r = run("shadow", ASK, "shadow", spec_over={"fix_attempts": 1})
    rows = [json.loads(x) for x in shadow.read_text().splitlines()] if shadow.exists() else []
    check("R7 shadow: no exit 3, flow as today (FAIL, no fix)", r.returncode, 1)
    check("R7 shadow: accepted row logged (stdout + jsonl)", "NEEDS-INFO-SHADOW accepted" in r.stdout and len(rows) == 1 and rows[0]["accepted"] is True, True)
    check("R7 shadow: NEEDS.md deleted", not (d / "work" / "NEEDS.md").exists(), True)
    d, r = run("off", ASK, None, spec_over={"fix_attempts": 1})
    check("R7 off: no pause, NEEDS.md deleted, same verdict as shadow", r.returncode == 1 and "NEEDS-INFO" not in r.stdout and not (d / "work" / "NEEDS.md").exists(), True)
    d, r = run("off-fix", {**FIX, "needs": NEEDS}, None)
    check("R7 off: NEEDS.md does not stop a green fix (PASS)", r.returncode == 0 and "RESULT PASS" in r.stdout, True)
    check("R7 off: PASS diff has no NEEDS.md", "NEEDS.md" not in (d / "REVIEW.md").read_text(), True)

    # R8 reject carry: attempt 1 asks badly, attempt 2 sees why and fixes
    carry = {"A": {}, "B": {}, "by_task": [{"match": "needs-info question rejected", **FIX},
                                           {"match": "WORKING DIRECTORY", "needs": NEEDS, "cmds": ["echo hi"]}]}
    d, r = run("carry", carry, "on")
    check("R8 rejected ask: next attempt's prompt carries the reason and can PASS", r.returncode == 0 and "RESULT PASS" in r.stdout and "phase B attempt 2" in r.stdout, True)

    # R9 prepare + R10 test sha
    def pre_ans(sha_):
        def f(d):
            Path(str(d / "TASK.md") + ".answers.md").write_text(f"### Q1: Which rounding?\nHalf up, always.\n<!-- test-sha: {sha_} -->\n")
            (d / "work.needs-info.json").write_text("{}")
        return f
    real_sha = R.sha(Path(HERE / "nonexistent")) if False else None
    import hashlib
    good_sha = hashlib.sha256(R.GOOD_T.encode()).hexdigest()
    d, r = run("answered", FIX, "on", pre=pre_ans(good_sha))
    tm = (d / "work" / "TASK.md").read_text()
    check("R9 answers appended to TASK.md under the heading", "## Answers to your earlier questions" in tm and "Half up, always." in tm and "test-sha" not in tm, True)
    check("R9 stale marker wiped, rerun PASSes with the same test", not (d / "work.needs-info.json").exists() and r.returncode == 0 and "RESULT PASS" in r.stdout, True)
    check("R9 the spec task file itself is untouched", "Answers" not in (d / "TASK.md").read_text(), True)
    d, r = run("shachanged", FIX, "on", pre=pre_ans("0" * 64))
    check("R10 changed test sha on rerun -> FAIL SPEC test-changed-while-parked", r.returncode == 1 and "test-changed-while-parked" in r.stdout and "class=SPEC" in r.stdout, True)
    # asked once already: a second ask is over the cap (rejected, not parked)
    d, r = run("cap", ASK, "on", pre=pre_ans(good_sha), spec_over={"fix_attempts": 1})
    check("R6 second ask after an answer -> cap rejection, not a park", r.returncode == 1 and "ask cap reached" in r.stdout, True)

    # full loop: park -> mini-answer (fake queue) -> the same job's rerun sees the answer, same frozen test -> PASS
    d, r = run("loop", ASK, "on")
    pm = json.loads((d / "work.needs-info.json").read_text())["pause_meta"]
    (d / "state.json").write_text(json.dumps({"jobs": [{"id": "loopjob00001", "label": "loop", "status": "paused", "pause_reason": "needs_info", "pause_meta": pm}]}))
    (d / "fakeq").write_text("#!/bin/sh\nexit 0\n")
    (d / "fakeq").chmod(0o755)
    ra = subprocess.run([sys.executable, str(HERE / "mini-answer"), "loopjob", "Round half up."], capture_output=True, text=True,
                        env={**os.environ, "MINI_ANSWER_STATE": str(d / "state.json"), "MINI_ANSWER_QUEUE": str(d / "fakeq")})
    check("loop: mini-answer writes the sidecar next to the spec task and resumes", ra.returncode == 0 and "Round half up." in Path(str(d / "TASK.md") + ".answers.md").read_text(), True)
    (d / "plan.json").write_text(json.dumps(FIX))
    env = {**os.environ, "MINI_BIN": str(stub), "DARKBLOOM_LOCAL_JSON": str(D / "local.json"), "STUB_PLAN": str(d / "plan.json"), "MINI_NEEDS_INFO": "on",
           "MINI_NEEDS_INFO_SHADOW_LOG": str(shadow)}
    r2 = subprocess.run([sys.executable, str(RUNNER), "--cwd", str(d / "work"), "--task-file", str(d / "spec.json")], capture_output=True, text=True, env=env)
    check("loop: the rerun passes with the answer in TASK.md and the test sha unchanged", r2.returncode == 0 and "RESULT PASS" in r2.stdout
          and "Round half up." in (d / "work" / "TASK.md").read_text() and "test-changed-while-parked" not in r2.stdout, True)

    # D1 diagnose mode
    import test_diag_check as TD
    DSPEC = {"mode": "diagnose", "allowed": [], "timeout": 600, "diag_attempts": 2, "locate": ["acquireBrowserLock"],
             "must_mention": ["acquireBrowserLock", "writeLock", "handleCommand"]}
    DNEEDS = NEEDS.replace("Which rounding mode does the vendor use for add?", "Which TTL does the vendor guarantee for the lock lease?").replace("round", "lease").replace("calc.js", "sidecar/lib.js")
    DASK = {"B": {}, "needs": DNEEDS, "cmds": ["grep -rn lease .", "cat sidecar/lib.js"]}

    def drun(name, plan, mode, pre=None):
        d = D / name
        (d / "work").mkdir(parents=True)
        fx = TD.mkrepo(d / "fixture")
        (d / "TASK.md").write_text("# TASK\nSymptom: EEXIST from acquireBrowserLock after restart. Write DIAGNOSIS.md.\n")
        (d / "spec.json").write_text(json.dumps({**DSPEC, "baseline_from": str(fx), "task": str(d / "TASK.md")}))
        (d / "plan.json").write_text(json.dumps(plan))
        if pre:
            pre(d)
        env = {**os.environ, "MINI_BIN": str(stub), "DARKBLOOM_LOCAL_JSON": str(D / "local.json"), "STUB_PLAN": str(d / "plan.json"),
               "MINI_NEEDS_INFO_FILE": str(D / "none"), "MINI_NEEDS_INFO_SHADOW_LOG": str(shadow)}
        env.pop("MINI_NEEDS_INFO", None)
        if mode:
            env["MINI_NEEDS_INFO"] = mode
        return d, subprocess.run([sys.executable, str(RUNNER), "--cwd", str(d / "work"), "--task-file", str(d / "spec.json")], capture_output=True, text=True, env=env)

    d, r = drun("diag-park", DASK, "on")
    mk = d / "work.needs-info.json"
    check("D1 diagnose + valid NEEDS -> exit 3 with a needs_info marker",
          r.returncode == 3 and mk.exists() and json.loads(mk.read_text())["pause_reason"] == "needs_info" and "RESUMABLE TRANSCRIPT" in r.stdout, True)
    d, r = drun("diag-off", DASK, None)
    check("D1 off: NEEDS.md ignored, diagnose fails as today (no pause)", r.returncode == 1 and "needs-info" not in r.stdout.lower().replace("needs_info", ""), True)
    d, r = drun("diag-ans", {"B": {}}, "on", pre=lambda d: Path(str(d / "TASK.md") + ".answers.md").write_text("### Q1: ttl?\n30s.\n"))
    check("D1 answers reach the diagnose TASK.md", "30s." in (d / "work" / "TASK.md").read_text(), True)

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAIL")
sys.exit(1 if FAILS else 0)
