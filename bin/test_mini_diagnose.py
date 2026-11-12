#!/usr/bin/env python3
"""Runner-level tests of mode "diagnose" with a stub MINI_BIN (no network, no GPU): python3 ~/bin/test_mini_diagnose.py"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_diag_check as T  # noqa: E402  (fixture repo + GOOD report)
import importlib.machinery  # noqa: E402
import importlib.util  # noqa: E402

RUNNER = Path(os.environ.get("MINI_RUNNER_UNDER_TEST") or HERE / "mini-swe-runner.py")  # a candidate copy can be tested before it goes live
_sp = importlib.util.spec_from_file_location("mini_swe_runner_diag", RUNNER,
                                             loader=importlib.machinery.SourceFileLoader("mini_swe_runner_diag", str(RUNNER)))
R = importlib.util.module_from_spec(_sp)
_sp.loader.exec_module(R)

STUB = r'''#!/usr/bin/env python3
import json, os, sys, time
a = sys.argv; task = a[a.index("-t") + 1]; traj = a[a.index("-o") + 1]
cwd = next(x.split("=", 1)[1] for x in a if x.startswith("environment.cwd="))
plan = json.load(open(os.environ["STUB_PLAN"]))
cnt = plan["counter"]
n = int(open(cnt).read()) + 1 if os.path.exists(cnt) else 1
open(cnt, "w").write(str(n))
limit = next((x.split("=", 1)[1] for x in a if x.startswith("agent.step_limit=")), "")
ovs = ",".join(os.path.basename(a[i + 1]) for i, x in enumerate(a[:-1]) if x == "-c" and a[i + 1].endswith(".yaml"))
open(plan["tasks"], "a").write(f"=== ATTEMPT {n} step_limit={limit} overlays={ovs}\n{task}\n")
step = dict(plan["attempts"][min(n, len(plan["attempts"])) - 1])
cmds = step.pop("_cmds", [])
loop, nap = step.pop("_loop", []), step.pop("_sleep", 0)
for rel, txt in step.items():
    os.makedirs(os.path.dirname(os.path.join(cwd, rel)) or cwd, exist_ok=True)
    open(os.path.join(cwd, rel), "w").write(txt)
if loop:  # completed steps (command + tool output) in the trajectory, then a hang: only the runner's watch ends it early
    lm = []
    for i, (c, o) in enumerate(loop):
        lm += [{"role": "assistant", "content": "checking", "extra": {"actions": [{"command": c.replace("CWD", cwd), "tool_call_id": f"t{i}"}]}},
               {"role": "tool", "tool_call_id": f"t{i}", "content": o}]
    json.dump({"messages": lm}, open(traj, "w"))
    time.sleep(nap)
now = int(time.time())
msgs = [{"role": "assistant", "content": "The lock is written by writeLock after a stale check that never unlinks the old file, so EEXIST follows.",
         "extra": {"actions": [{"command": c.replace("CWD", cwd)} for c in cmds]}} for _ in [0] if cmds]
msgs.append({"role": "assistant", "content": "done", "extra": {"response": {"created": now, "usage": {"prompt_tokens": 100, "completion_tokens": 10}}}})
json.dump({"messages": msgs}, open(traj, "w"))
print("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")
'''

SPEC = {"mode": "diagnose", "allowed": [], "timeout": 600, "diag_attempts": 3,
        "locate": ["acquireBrowserLock"], "must_mention": ["acquireBrowserLock", "writeLock", "handleCommand"]}


def run_case(root, name, repo, attempts, spec_extra=None):
    run = Path(root) / name
    (run / "work").mkdir(parents=True)
    task = run / "task.md"
    task.write_text("# TASK\nSymptom: EEXIST from acquireBrowserLock after restart. Write DIAGNOSIS.md.\n")
    spec = {**SPEC, "baseline_from": str(repo), "task": str(task), **(spec_extra or {})}
    (run / "spec.json").write_text(json.dumps(spec))
    (run / "plan.json").write_text(json.dumps({"attempts": attempts, "counter": str(run / "count"), "tasks": str(run / "tasks.txt")}))
    local = Path(root) / "local.json"
    local.write_text(json.dumps({"base_url": "http://127.0.0.1:9/v1", "api_key": "k-SECRET"}))
    stub = Path(root) / "stub-mini"
    stub.write_text(STUB)
    stub.chmod(0o755)
    r = subprocess.run([sys.executable, str(RUNNER), "--cwd", str(run / "work"), "--task-file", str(run / "spec.json")],
                       capture_output=True, text=True, timeout=300,
                       env={**os.environ, "STUB_PLAN": str(run / "plan.json"), "MINI_BIN": str(stub), "DARKBLOOM_LOCAL_JSON": str(local)})
    res = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")]
    count = int((run / "count").read_text()) if (run / "count").exists() else 0
    return r, res, run, count


def need(cond, msg, r=None):
    if not cond:
        print("FAIL " + msg)
        if r is not None:
            print(r.stdout[-2500:], r.stderr[-800:])
        sys.exit(1)
    print("ok   " + msg)


def main():
    with tempfile.TemporaryDirectory() as root:
        repo = T.mkrepo(Path(root) / "fixture")
        good = T.GOOD

        # 1. first attempt writes nothing, the retry writes a valid report -> PASS after 2 attempts
        r, res, run, n = run_case(root, "retry-pass", repo, [{}, {"DIAGNOSIS.md": good}])
        need(r.returncode == 0 and len(res) == 1 and res[0].startswith("RESULT PASS mode=diagnose attempts=2"), f"empty then valid -> PASS attempts=2 ({res})", r)
        need(n == 2, "stub ran exactly twice")
        tasks = (run / "tasks.txt").read_text()
        need("DIAGNOSIS.md" in tasks.split("=== ATTEMPT 2")[1] and "no usable DIAGNOSIS.md" in tasks.split("=== ATTEMPT 2")[1], "retry prompt says no usable report")
        need("cat > DIAGNOSIS.md" in tasks and "READ-ONLY" in tasks, "first prompt carries the contract")
        print("     starting points in prompt:", "## Starting points" in tasks)
        rv = (run / "REVIEW.md").read_text()
        need("REVIEW packet: PASS" in rv and "attempt 2" in rv, "REVIEW.md written")
        need("k-SECRET" not in r.stdout, "key not echoed")

        # 2. rejected report (fabricated quote) then a fixed one -> retry prompt carries the failure text, file kept
        bad = good.replace("    writeLock(file, holder);\n  }\n  return holder;", "    unlinkAndWrite(file, holder);\n  }\n  return holder;")
        r, res, run, n = run_case(root, "reject-then-pass", repo, [{"DIAGNOSIS.md": bad}, {"DIAGNOSIS.md": good}])
        t2 = (run / "tasks.txt").read_text().split("=== ATTEMPT 2")[1]
        need(r.returncode == 0 and res and res[0].startswith("RESULT PASS mode=diagnose attempts=2"), f"rejected then fixed -> PASS ({res})", r)
        need("DIAGNOSIS.md was REJECTED" in t2 and "citation sidecar/lib.js:" in t2 and "REAL lines" in t2 and "do not rewrite from scratch" in t2,
             "retry prompt lists the rejection with the real lines")
        # diag_reject_note drops the invented quote so the model does not re-copy it (the gate log still names it)
        need("unlinkAndWrite" not in t2 and "unlinkAndWrite" in r.stdout, "retry prompt drops the invented quote")

        # 3. stub edits a tracked source file: it is restored and the report is judged against the real code (3435e2a1265b: the
        #    model "fixed" the bug while diagnosing and the whole job died; an edit is now a rejected attempt, not the end)
        r, res, run, n = run_case(root, "scope-valid", repo, [{"DIAGNOSIS.md": good, "sidecar/lib.js": "// tampered\n"}])
        need(r.returncode == 0 and res and res[0].startswith("RESULT PASS mode=diagnose attempts=1"), f"tracked edit + valid report -> PASS ({res})", r)
        need((run / "work/sidecar/lib.js").read_text() == T.LIB, "tampered file restored")
        need("scope: edited ['sidecar/lib.js'] (restored)" in (run / "REVIEW.md").read_text(), "REVIEW.md records the restored edit")
        r, res, run, n = run_case(root, "scope-retry", repo, [{"DIAGNOSIS.md": bad, "sidecar/lib.js": "// tampered\n"}, {"DIAGNOSIS.md": good}])
        t2 = (run / "tasks.txt").read_text().split("=== ATTEMPT 2")[1]
        need(r.returncode == 0 and res and res[0].startswith("RESULT PASS mode=diagnose attempts=2") and n == 2, f"tracked edit + bad report -> retry -> PASS ({res})", r)
        need("you edited tracked source ['sidecar/lib.js']; it was restored" in t2 and "Diagnosis is read-only" in t2 and "REAL lines" in t2,
             "retry prompt names the restored edit and the real lines")
        need((run / "work/sidecar/lib.js").read_text() == T.LIB, "restored before the retry")
        r, res, run, n = run_case(root, "scope-exhausted", repo, [{"DIAGNOSIS.md": bad, "sidecar/lib.js": "// tampered\n"}], {"diag_attempts": 2})
        need(r.returncode == 1 and res and res[0].startswith("RESULT FAIL mode=diagnose check attempts=2/2: scope: you edited tracked source") and n == 2,
             f"edits every attempt -> FAIL check naming the scope problem ({res}, n={n})", r)
        need((run / "work/sidecar/lib.js").read_text() == T.LIB, "restored at the end")

        # 3b. drifted citation ranges (bench-dupes 5dc3adcb4f59: cited from a since-restored edit, every range ~9 lines off,
        #     2 attempts burned): the runner moves the ranges to the real quoted lines before the gate -> PASS on attempt 1
        drift = good.replace("sidecar/lib.js:12-18", "sidecar/lib.js:20-21")
        r, res, run, n = run_case(root, "drift", repo, [{"DIAGNOSIS.md": drift, "sidecar/lib.js": "// tampered\n"}])
        need(r.returncode == 0 and res and res[0].startswith("RESULT PASS mode=diagnose attempts=1") and n == 1, f"drifted ranges moved -> PASS attempts=1 ({res})", r)
        need("citation ranges moved to where the quoted lines are: sidecar/lib.js:20-21 -> sidecar/lib.js:12-21" in r.stdout, "the move is logged")
        need("```js sidecar/lib.js:12-21" in (run / "work/DIAGNOSIS.md").read_text(), "the accepted report carries the real range")
        need("ranges moved: 1" in (run / "REVIEW.md").read_text(), "REVIEW.md records the move")
        # 3c. shortened paths (scrapy r2 5f2fbdc72e8e: D1 rejected for `crawl.py:33` beside its own full citations): written in
        #     full before the gate, then the drifted range is moved -> PASS on attempt 1
        short = (good.replace("```js sidecar/lib.js:12-18", "```js ./lib.js:20-21").replace("write at sidecar/lib.js:9", "write at lib.js:9")
                 .replace("- File: `sidecar/lib.js`", "- File: `lib.js`"))
        assert short.count("sidecar/lib.js") == good.count("sidecar/lib.js") - 3, "the fixture lines changed"
        r, res, run, n = run_case(root, "short-paths", repo, [{"DIAGNOSIS.md": short}])
        need(r.returncode == 0 and res and res[0].startswith("RESULT PASS mode=diagnose attempts=1") and n == 1, f"short paths resolved -> PASS attempts=1 ({res})", r)
        need("diagnose: paths resolved: ./lib.js -> sidecar/lib.js; lib.js -> sidecar/lib.js" in r.stdout, "the rewrite is logged", r)
        need((run / "work/DIAGNOSIS.md").read_text() == good.replace("```js sidecar/lib.js:12-18", "```js sidecar/lib.js:12-21"),
             "the accepted report names the full paths and the real range")
        need("paths resolved: 2" in (run / "REVIEW.md").read_text(), "REVIEW.md records the rewrites")
        # 3d. an abbreviated multi-line statement (optuna r2 141aee2f8f34: D3's one defect was `raise ValueError(...)` for a real
        #     4-line raise) -> accepted on attempt 1
        elided = good.replace("  if (!isPidAlive(cur.pid)) {\n    // stale: break it\n    writeLock(file, holder);\n  }\n", "  if (!isPidAlive(cur.pid)) { ... }\n")
        assert elided != good
        r, res, run, n = run_case(root, "elided", repo, [{"DIAGNOSIS.md": elided}])
        need(r.returncode == 0 and res and res[0].startswith("RESULT PASS mode=diagnose attempts=1") and n == 1, f"elided statement -> PASS attempts=1 ({res})", r)
        # 3e. a quoted line mis-copied in its quote characters (bibtexr2 5329a2a546bb: all three attempts rejected for a regex
        #     with one ' too many and a string with one backtick dropped) -> the line takes the file's text, PASS on attempt 1
        cur = "  const cur = JSON.parse(fs.readFileSync(file, 'utf8'));"
        assert good.count(cur + "\n") == 1
        mangled = good.replace(cur + "\n", cur.replace("'utf8'", "\"utf8'") + "\n")
        r, res, run, n = run_case(root, "quotes", repo, [{"DIAGNOSIS.md": mangled}])
        need(r.returncode == 0 and res and res[0].startswith("RESULT PASS mode=diagnose attempts=1") and n == 1, f"mis-copied quote restored -> PASS attempts=1 ({res})", r)
        need("diagnose: quoted lines restored to the file's text: sidecar/lib.js:12-18: " in r.stdout, "the restore is logged", r)
        need((run / "work/DIAGNOSIS.md").read_text() == good, "the accepted report quotes the file's text")
        need("quotes restored: 1" in (run / "REVIEW.md").read_text(), "REVIEW.md records the restore")
        # retry note for a quote outside the cited range (beyond fix_ranges' reach): it says where the line is
        note = R.diag_reject_note(str(repo), ["citation sidecar/lib.js:1-3 (report line 15): quoted line not found in lines 1-8: "
                                              "'function acquireBrowserLock(file, holder) {' (it is at line 12)"])
        need("outside the cited range (it is at line 12): change the range" in note and "invented" not in note and "REAL lines 1-8" in note,
             "retry note names where an out-of-range quote is")
        note = R.diag_reject_note(str(repo), ["citation sidecar/lib.js:12-18 (report line 15): quoted line not found in lines 7-23: "
                                              "'unlinkAndWrite(file, holder);' (it is nowhere in sidecar/lib.js)"])
        need("not in the file (invented text removed)" in note and "unlinkAndWrite" not in note, "retry note still drops an invented quote")
        # 3f. the contract's `<path>` copied literally over real quotes, and the fix's new line cited as if it were in the file
        #     (dmcg1 r2 222308c65fb9, conan2 r1 014d9fdb192b): the path is found by its lines, the proposed code un-cited -> PASS attempt 1
        ph = (good.replace("```js sidecar/poll.js:2-4", "```js <path>:2-4")
              .replace("Unlink the stale lock file", "```js sidecar/lib.js:13\n  if (fs.existsSync(file)) fs.unlinkSync(file);\n```\n\nUnlink the stale lock file", 1))
        assert ph.count("<path>") == 1 and "fs.unlinkSync" in ph
        r, res, run, n = run_case(root, "placeholder-proposed", repo, [{"DIAGNOSIS.md": ph}])
        need(r.returncode == 0 and res and res[0].startswith("RESULT PASS mode=diagnose attempts=1") and n == 1, f"<path> resolved, proposed code un-cited -> PASS attempts=1 ({res})", r)
        need("diagnose: paths resolved: <path> -> sidecar/poll.js" in r.stdout, "the content resolution is logged", r)
        need("diagnose: proposed-code fences un-cited (the fix's or the test's new code): sidecar/lib.js:13 (report line " in r.stdout, "the un-citing is logged", r)
        rv = (run / "REVIEW.md").read_text()
        need("paths resolved: 1" in rv and "proposed code un-cited: 1" in rv, "REVIEW.md records both repairs")

        # 4. never produces a valid report -> FAIL after diag_attempts
        r, res, run, n = run_case(root, "exhausted", repo, [{"DIAGNOSIS.md": "Let me think about it.\n"}], {"diag_attempts": 2})
        need(r.returncode == 1 and res and res[0].startswith("RESULT FAIL mode=diagnose check") and n == 2, f"always invalid -> FAIL after 2 attempts ({res}, n={n})", r)

        # 5. step budget + carry-forward (cc6618122c9b: 3 x 40 steps of reading, no report): default 80 steps, draft-early
        #    instruction, and an attempt that wrote nothing hands its reads and notes to the next one
        r, res, run, n = run_case(root, "carry", repo, [{"_cmds": ["nl -ba CWD/sidecar/lib.js | sed -n '1,30p'", "grep -n writeLock CWD/sidecar/lib.js"]},
                                                        {"DIAGNOSIS.md": good}])
        t = (run / "tasks.txt").read_text()
        a1, a2 = t.split("=== ATTEMPT 2")
        need(res and res[0].startswith("RESULT PASS mode=diagnose attempts=2"), f"carry case passes ({res})", r)
        need("step_limit=80" in a1 and "step_limit=80" in a2, "default diag_steps is 80")
        need("STEP BUDGET: you have 80 steps" in a1 and "By step 20" in a1 and "STEP BUDGET" in a2, "both prompts carry the draft-early budget")
        need("Read: sidecar/lib.js:1-30" in a2 and "grep -n writeLock sidecar/lib.js" in a2 and "never unlinks" in a2, "retry prompt carries reads, searches and notes")
        need("did this; do NOT redo" not in a1, "first prompt has no carry block")
        need("## Starting points" in a2 or "## Starting points" not in a1, "retry keeps the starting points")
        r, res, run, n = run_case(root, "steps-override", repo, [{"DIAGNOSIS.md": good}], {"diag_steps": 30})
        need("step_limit=30" in (run / "tasks.txt").read_text() and "you have 30 steps" in (run / "tasks.txt").read_text(), "spec diag_steps overrides the default")

        # 6. form repair (ranges 6414eb3abf32: the last of 3 reports was rejected only for a 507-char Root cause; since stage 18
        #    the gate allows 600, so the case uses one of about 680): every attempt
        #    used and only form failures left -> ONE extra short attempt that is told to fix the form alone; any content failure,
        #    or a failed repair, ends the run as before
        rc0 = "acquireBrowserLock treats a stale holder by calling writeLock with flag wx without first unlinking the old file, so the write throws EEXIST."
        assert rc0 in good
        long_rc = good.replace(rc0, rc0[:-1] + ", " + "which matters because the sidecar restarts often and every restart leaves a stale lock behind for the next poll cycle to trip over " * 4 + "again.")
        two_rc = good.replace(rc0, rc0 + " The poll loop then retries forever.")
        r, res, run, n = run_case(root, "form-repair", repo, [{"DIAGNOSIS.md": long_rc}, {"DIAGNOSIS.md": two_rc}, {"DIAGNOSIS.md": good}], {"diag_attempts": 2})
        t = (run / "tasks.txt").read_text()
        a3 = t.split("=== ATTEMPT 3")[1] if "=== ATTEMPT 3" in t else ""
        need(r.returncode == 0 and res and res[0].startswith("RESULT PASS mode=diagnose attempts=3") and n == 3, f"form-only failures -> one repair attempt -> PASS ({res}, n={n})", r)
        need("step_limit=8" in a3.split("\n")[0] and "only its FORM was rejected" in a3 and "Root cause must be ONE sentence" in a3
             and "STEP BUDGET" not in a3 and "## Starting points" not in a3, "the repair prompt: 8 steps, the form failures, no budget or starting points")
        # dupes3 69c0a9690916: the repair got the contract and TASK.md again and spent its 8 steps re-reading source
        a1h = t.split("=== ATTEMPT 1")[1].split("\n")[0]
        need("mini-diagnose-polish.yaml" in a3.split("\n")[0] and "mini-diagnose.yaml" in a1h and "polish" not in a1h,
             f"the repair runs under the polish overlay, the attempts under the diagnose one ({a1h!r} / {a3.splitlines()[0]!r})")
        need("--- TASK.md ---" not in a3 and "## Code path" not in a3 and rc0 + " The poll loop then retries forever." in a3
             and R.DIAG_POLISH_PLACEHOLDER in a3 and "python3 - <<'PY'" in a3 and "WORKING DIRECTORY" in a3,
             "the repair prompt carries the rejected Root cause and the recipe, not the task or the contract")
        drifted = good.replace("## Second cause\n", "## Second cause\n\nAlso the poll loop never backs off.\n", 1)
        assert drifted != good
        r, res, run, n = run_case(root, "form-repair-drift", repo, [{"DIAGNOSIS.md": long_rc}, {"DIAGNOSIS.md": two_rc}, {"DIAGNOSIS.md": drifted}], {"diag_attempts": 2})
        need(r.returncode == 1 and res and "form repair changed other sections: Second cause" in res[0] and n == 3,
             f"a repair that changes another section fails ({res}, n={n})", r)
        need("form repair: exit=" in (run / "REVIEW.md").read_text(), "REVIEW.md marks the repair attempt")
        r, res, run, n = run_case(root, "form-repair-fails", repo, [{"DIAGNOSIS.md": long_rc}], {"diag_attempts": 2})
        need(r.returncode == 1 and res and res[0].startswith("RESULT FAIL mode=diagnose check attempts=2/2 +form repair: Root cause is") and n == 3,
             f"a failed repair -> FAIL, one extra attempt only ({res}, n={n})", r)
        mixed = long_rc.replace("    writeLock(file, holder);\n  }\n  return holder;", "    unlinkAndWrite(file, holder);\n  }\n  return holder;")
        assert mixed != long_rc
        r, res, run, n = run_case(root, "form-and-content", repo, [{"DIAGNOSIS.md": mixed}], {"diag_attempts": 2})
        need(r.returncode == 1 and res and res[0].startswith("RESULT FAIL mode=diagnose check attempts=2/2:") and n == 2,
             f"a content failure next to the form one -> no repair attempt ({res}, n={n})", r)
        need(R.diag_form_only(["Root cause is 507 chars (> 400): one sentence"]) and R.diag_form_only(["report starts with narration (Let me...): write only the report"])
             and not R.diag_form_only([]) and not R.diag_form_only(["Root cause is 507 chars (> 400): one sentence", "missing section '## Second cause'"])
             and not R.diag_form_only(["Root cause is empty"]), "diag_form_only")

        # 7. cfgkeys 2cdee16a8b71 (three runner gaps behind one failed diagnose):
        #  a. the reject note: eight per-line repeats of one citation failure filled the 3000-char cap and cut the one content
        #     failure (`Function: DEFAULTS`) out of the retry prompt -> said once each, non-citation problems first, cut between
        #     problems with a count
        cit = [f"citation sidecar/lib.js:12-18 (report line 15): quoted line not found in lines 7-23: 'made up {k}' (it is nowhere in sidecar/lib.js)"
               for k in range(8)]
        head = "Minimal fix: sidecar/lib.js does not define function LOCKS"
        note = R.diag_reject_note(str(repo), cit + [head])
        need(note.startswith(head) and note.count("invented text removed") == 1 and note.count("REAL lines ") == 1 and "made up" not in note,
             f"reject note: the content failure first, the repeated citation once, its real lines once ({note[:160]!r})")
        cut = R.diag_reject_note(str(repo), [head, "Root cause is 507 chars (> 400): one sentence"] + cit, limit=200)
        need(cut.startswith(head) and "Root cause is 507" in cut and "more problem(s) not shown" in cut and len(cut) < 400,
             f"reject note: cut between problems, with a count of what was cut ({cut!r})")
        need(R.diag_reject_note(str(repo), ["x" * 500], limit=100) == "x" * 100, "reject note: one problem longer than the cap is cut at the cap")
        #  b. the carry header of a REJECTED report must not claim the attempt ran out of steps before writing one
        tj = Path(root) / "carry.traj.json"
        tj.write_text(json.dumps({"messages": [{"role": "assistant", "content": "The stale check never unlinks the lock file.",
                                                "extra": {"actions": [{"command": f"grep -n writeLock {repo}/sidecar/lib.js"}]}}]}))
        cw, cn = R.diag_carry(tj, str(repo), wrote=True), R.diag_carry(tj, str(repo))
        need("already did this exploration" in cw and "ran out of steps" not in cw and "Searched: grep -n writeLock sidecar/lib.js" in cw, f"carry after a rejected report ({cw[:120]!r})")
        need("ran out of steps BEFORE writing DIAGNOSIS.md" in cn, "carry after no report keeps its header")
        #  c. a diagnose attempt stuck re-running one command is stopped by the watch (it had none: D2 ran one grep ~20 times), and
        #     the next attempt is told why
        t0 = time.time()
        loop = [["grep -n writeLock CWD/sidecar/lib.js", "12:    writeLock(file, holder);"]] * 3
        r, res, run, n = run_case(root, "watch", repo, [{"_loop": loop, "_sleep": 120}, {"DIAGNOSIS.md": good}])
        took = time.time() - t0
        a2 = (run / "tasks.txt").read_text().split("=== ATTEMPT 2")[1] if n >= 2 else ""
        need(r.returncode == 0 and res and res[0].startswith("RESULT PASS mode=diagnose attempts=2") and n == 2 and took < 60,
             f"a looping attempt is stopped early, the retry passes ({res}, n={n}, {took:.0f}s)", r)
        need("diagnose attempt 1 stopped: fail-fast: the same command ran 3 times in a row" in r.stdout, "the stop is logged")
        need("The previous attempt was STOPPED: fail-fast: the same command ran 3 times in a row" in a2 and "Never re-run a command" in a2
             and "grep -n writeLock sidecar/lib.js" in a2, "the retry prompt says why the last attempt stopped, plus its carry")
        need("exit=killed stopped: repeated command report=none" in (run / "REVIEW.md").read_text(), "REVIEW.md records the stop")
    print("ALL PASS")


if __name__ == "__main__":
    main()
