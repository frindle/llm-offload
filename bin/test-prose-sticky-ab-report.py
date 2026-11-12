#!/usr/bin/env python3
"""Regression test for prose-sticky-ab-report.py (the PROSE_STICKY_NOTHINK A/B reader + decision rule).
Pure fixtures: synthetic job logs + a synthetic dispatch-metrics.jsonl in a temp dir; no queue, no GPU, no network.
  python3 test-prose-sticky-ab-report.py                run
  python3 test-prose-sticky-ab-report.py --revert-check mutate the report 9 ways; every mutant must turn the test RED"""
import importlib.util, json, os, sys, tempfile, subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("REPORT_SRC") or HERE / "prose-sticky-ab-report.py")
spec = importlib.util.spec_from_file_location("psabr", SRC)
R = importlib.util.module_from_spec(spec); spec.loader.exec_module(R)
FAILS = []


def check(name, cond, note=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  -- {note}" if note and not cond else ""))
    if not cond:
        FAILS.append(name)


def run(arm, passed=True, aborts=0, cuts=0, wall=100.0, iters=8, valid=True):
    return {"arm": arm, "valid": valid, "invalid_why": None, "passed": passed and valid, "iters": iters, "max_iters": 20,
            "prose_aborts": aborts, "cap_cuts": cuts, "terminal": None, "l4_engaged": 1 if aborts else 0, "wall_s": wall,
            "prompt_tok": 1000, "completion_tok": 500, "tok_s": 50.0, "decode_tok_s": None,
            "first_author_thinking": True, "later_author_thinking_off": None}


def mk(a_specs, x_specs, x="B"):
    """specs: list of (task, kwargs). Two reps per task."""
    runs = {}
    for arm, specs in (("A", a_specs), (x, x_specs)):
        for task, kw in specs:
            for rep in (1, 2):
                runs[(arm, rep, task)] = run(arm, **kw)
    return runs


def verdict_of(runs, x="B"):
    return R.verdict(R.summarize(runs), R.per_task(runs))[x]["verdict"]


TASKS = [f"t{i}" for i in range(6)]                           # 6 tasks x 2 reps = 12 valid runs per arm (== MIN_VALID)
A_BASE = [(t, dict(passed=i < 4, aborts=1 if i < 3 else 0)) for i, t in enumerate(TASKS)]


def x_specs(**over):
    out = []
    for i, t in enumerate(TASKS):
        kw = dict(passed=i < 4, aborts=0, cuts=0, wall=100.0)
        out.append((t, {**kw, **over.get(t, {}), **over.get("*", {})}))
    return out


# ---- decision rule ---------------------------------------------------------------------------------------------------------
check("flip candidate when all five checks hold", verdict_of(mk(A_BASE, x_specs())) == "FLIP-CANDIDATE")
check("pass rate lower -> not worth it", verdict_of(mk(A_BASE, x_specs(t0={"passed": False}))) == "NOT-WORTH-IT")
check("aborts equal (not strictly fewer) -> not worth it",
      verdict_of(mk(A_BASE, [(t, dict(passed=i < 4, aborts=1 if i < 3 else 0)) for i, t in enumerate(TASKS)])) == "NOT-WORTH-IT")
check("more output-cap cut-offs -> not worth it", verdict_of(mk(A_BASE, x_specs(t5={"cuts": 1}))) == "NOT-WORTH-IT")
check("wall 1.30x control -> not worth it", verdict_of(mk(A_BASE, x_specs(**{"*": {"wall": 130.0}}))) == "NOT-WORTH-IT")
check("wall 1.20x control still allowed", verdict_of(mk(A_BASE, x_specs(**{"*": {"wall": 120.0}}))) == "FLIP-CANDIDATE")
# higher pass rate overall but loses on a task => wins >= losses still holds when it wins more
check("pass wins>=losses evaluated per task",
      verdict_of(mk(A_BASE, x_specs(t0={"passed": False}, t4={"passed": True}, t5={"passed": True}))) == "FLIP-CANDIDATE")
runs = mk(A_BASE, x_specs(t0={"passed": False}, t1={"passed": False}, t2={"passed": False}, t4={"passed": True}, t5={"passed": True}))
check("pass rate lower and losses>wins -> not worth it", verdict_of(runs) == "NOT-WORTH-IT")
# equal pass totals (rule 1 holds) but X wins one task by 2 reps and loses two tasks by 1 rep each -> wins 1 < losses 2
wl = {}
for t, (pa, px) in {"w0": (0, 2), "w1": (2, 1), "w2": (2, 1), "w3": (2, 2), "w4": (2, 2), "w5": (0, 0)}.items():
    for rep in (1, 2):
        wl[("A", rep, t)] = run("A", passed=rep <= pa, aborts=1)
        wl[("B", rep, t)] = run("B", passed=rep <= px, aborts=0)
sw = R.summarize(wl)
check("wins/losses fixture: pass rates equal", sw["A"]["pass"] == sw["B"]["pass"] + 0 or sw["A"]["pass"] <= sw["B"]["pass"], str(sw))
check("per-task wins < losses blocks a flip even when pass rate is not lower", verdict_of(wl) == "NOT-WORTH-IT", verdict_of(wl))
no_abort_a = [(t, dict(passed=i < 4, aborts=0)) for i, t in enumerate(TASKS)]
check("control with zero aborts -> NO-SIGNAL (never a flip)", verdict_of(mk(no_abort_a, x_specs())) == "NO-SIGNAL")
check("too few valid runs -> INCONCLUSIVE", verdict_of(mk(A_BASE[:4], x_specs()[:4])) == "INCONCLUSIVE")
inv = mk(A_BASE, x_specs())
inv[("B", 1, "t0")]["valid"] = False; inv[("B", 1, "t0")]["passed"] = False
check("invalid runs are excluded from the arm (11 valid -> INCONCLUSIVE)", verdict_of(inv) == "INCONCLUSIVE")
check("arm C judged by the same rule", verdict_of(mk(A_BASE, x_specs(), x="C"), x="C") == "FLIP-CANDIDATE")
s = R.summarize(mk(A_BASE, x_specs()))
check("summary counts aborts over valid runs only", s["A"]["prose_aborts"] == 6 and s["B"]["prose_aborts"] == 0, str(s["A"]))
check("summary pass rate", s["A"]["pass_rate"] == round(8 / 12, 3), str(s["A"]))

# ---- parse_run -------------------------------------------------------------------------------------------------------------
PROF = 'role=author api=openai sent={"chat_template_kwargs": {"enable_thinking": %s}, "max_tokens": 32768}'
LOG_B = "\n".join([
    "[worker] WORKER FLAGS PROSE_STICKY_NOTHINK=1 (from the task's Worker-flags line).",
    "[worker] model profile: qwen thinking_coding mode=x (role=author) temp=1",
    "[worker] request profile=p " + PROF % "true",
    "[worker] iteration 3 was ABORTED by the prose-runaway guard (block of 500 chars repeated x3) with no tool call -- x",
    "[worker] L4: prose runaway seen - thinking stays OFF for the rest of this run",
    "[worker] request profile=p " + PROF % "false",
    "[worker] CHECKPOINT TRANSCRIPT: /t/x.json", "[worker] TERMINAL REASON: none"])
M = {"iterations": 7, "verify_passed": True, "prose_runaway_aborts": 1, "output_cap_prose_cuts": 0, "wall_time_s": 90.0,
     "sum_prompt_tokens": 1234, "sum_completion_tokens": 321, "tok_s_overall": 40.0, "max_iters": 20}
r = R.parse_run(LOG_B, M, "B")
check("parse: B run valid, passed, 1 abort, L4 engaged, wire thinking true then off",
      r["valid"] and r["passed"] and r["prose_aborts"] == 1 and r["l4_engaged"] == 1 and r["first_author_thinking"] is True
      and r["later_author_thinking_off"] is True, str(r))
check("parse: arm B log claimed as arm A is INVALID (flag mismatch)", not R.parse_run(LOG_B, M, "A")["valid"])
check("parse: arm B expects its flag; log without it is INVALID", not R.parse_run(LOG_B.replace("PROSE_STICKY_NOTHINK=1", "VERIFY_ON_EDIT=1"), M, "B")["valid"])
check("parse: arm C log carrying B's flag is INVALID", not R.parse_run(LOG_B, M, "C")["valid"])
check("parse: B log that ALSO carries C's flag is INVALID (no cross-arm leak)", not R.parse_run(LOG_B.replace("PROSE_STICKY_NOTHINK=1", "PROSE_STICKY_NOTHINK=1 NOTHINK_RUN=1"), M, "B")["valid"])
check("parse: no metrics row -> invalid", not R.parse_run(LOG_B, None, "B")["valid"])
check("parse: terminal prose_loop counts as a runaway even with 0 recorded aborts",
      R.parse_run(LOG_B, {**M, "prose_runaway_aborts": 0, "terminal_reason": "prose_loop", "verify_passed": False}, "B")["prose_aborts"] == 1)
LOG_A = LOG_B.replace("[worker] WORKER FLAGS PROSE_STICKY_NOTHINK=1 (from the task's Worker-flags line).\n", "")
check("parse: control log with no flag line is valid arm A", R.parse_run(LOG_A, M, "A")["valid"])

# ---- end to end through collect() / main() ----------------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    met = td / "m.jsonl"
    rows = []
    for arm in "ABC":
        for rep in (1, 2):
            tp = f"/t/{arm}{rep}.json"
            fl = {"A": "", "B": "[worker] WORKER FLAGS PROSE_STICKY_NOTHINK=1 (from the task's Worker-flags line).\n",
                  "C": "[worker] WORKER FLAGS NOTHINK_RUN=1 (from the task's Worker-flags line).\n"}[arm]
            (td / (f"{ord(arm):02x}{rep}000000000"[:12] + f"-psab-{arm}{rep}-aaaaaaaaaaaa.log")).write_text(
                fl + "[worker] model profile: q\n[worker] CHECKPOINT TRANSCRIPT: " + tp + "\n")
            rows.append({"transcript_path": tp, "iterations": 5, "verify_passed": arm != "A", "wall_time_s": 10.0, "prose_runaway_aborts": 2 if arm == "A" else 0})
    (td / "ignored-psab-A1-bbbbbbbbbbbb [auto-fix r1].log").write_text("x")      # auto-fix child: never matched
    # REAL child/rerun log names (queue-logs/<id>-<label>.log, label '<first> [auto-fix r1]' sanitised to '__auto-fix_r1_',
    # '-lc1' = lane-change retry; 'psc-' = the arm-C rerun bundle). Each carries a PASSING metrics row for arm C and the C flag, so if the
    # report ever counted one it would flip a failed first attempt to a pass / add a pass -- arm C inflation.
    for nm in ("cccccccccccc-psab-C1-aaaaaaaaaaaa__auto-fix_r1_.log", "dddddddddddd-psab-C1-aaaaaaaaaaaa__auto-fix_r1_-lc1.log",
               "eeeeeeeeeeee-psc-C1-aaaaaaaaaaaa.log", "ffffffffffff-psc-C1-aaaaaaaaaaaa__auto-fix_r1_.log"):
        (td / nm).write_text("[worker] WORKER FLAGS NOTHINK_RUN=1 (from the task's Worker-flags line).\n[worker] model profile: q\n"
                             "[worker] CHECKPOINT TRANSCRIPT: /t/child.json\n")
    rows.append({"transcript_path": "/t/child.json", "iterations": 5, "verify_passed": True, "wall_time_s": 1.0, "prose_runaway_aborts": 0})
    met.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
    runs, dupes = R.collect(str(td), str(met))
    check("collect: auto-fix / lane-change / psc-rerun child logs are NEVER counted (first attempts only; arm C not inflated)",
          len(runs) == 6 and not dupes and sorted(v["job"] for v in runs.values()).count("cccccccccccc") == 0, str(sorted(runs)))
    check("collect: 6 first attempts found, child log ignored", len(runs) == 6 and all(v["valid"] for v in runs.values()), str(list(runs)))
    check("collect: pass/aborts joined from metrics by transcript path",
          runs[("A", 1, "aaaaaaaaaaaa")]["passed"] is False and runs[("A", 1, "aaaaaaaaaaaa")]["prose_aborts"] == 2
          and runs[("B", 2, "aaaaaaaaaaaa")]["passed"] is True)
    out = subprocess.run([sys.executable, str(SRC), "--json", "--no-queue", "--logdir", str(td), "--metrics", str(met)], capture_output=True, text=True)
    check("main --json runs and reports INCONCLUSIVE for 2 valid runs/arm", out.returncode == 0 and '"INCONCLUSIVE"' in out.stdout, out.stderr[-300:])
    out = subprocess.run([sys.executable, str(SRC), "--no-queue", "--logdir", str(td), "--metrics", str(met)], capture_output=True, text=True)
    check("main table renders arm rows and the rule", out.returncode == 0 and "DECISION RULE" in out.stdout and "\nA " in out.stdout, out.stderr[-300:])

# ---- psab2 reader (PROTECT_HARNESS rerun: taint exclusion + Wilson intervals) -----------------------------------------------
lo, hi = R.wilson(8, 16)
check("psab2: wilson(8/16) ~ [0.28, 0.72]", abs(lo - 0.276) < 0.01 and abs(hi - 0.724) < 0.01, str((lo, hi)))
check("psab2: wilson(16/16) upper bound 1.0, lower < 1; n=0 -> (None, None)",
      R.wilson(16, 16)[1] == 1.0 and R.wilson(16, 16)[0] < 0.85 and R.wilson(0, 0) == (None, None), str(R.wilson(16, 16)))
REF = ('"""Reference impl for: demo\n\nThe gate applies this, runs the verify, and reverts it.\n"""\nimport pathlib\n'
       'OLD = """def api_list():\n    conn = _db()\n    rows = conn.execute("SELECT id, created_at FROM voicemails").fetchall()\n    conn.close()\n    return [dict(r) for r in rows]\n"""\n'
       'NEW = """def api_list():\n    rows = conn.execute("SELECT id, created_at, ov_vm, ov_shot FROM voicemails").fetchall()\n'
       '    out = []\n    for r in rows:\n        d = {**dict(r), "vm": bool(r["ov_vm"]), "shot": bool(r["ov_shot"])}\n'
       '        d["screenshot_paths"] = json.loads(r["screenshot_paths"] or "[]")\n        out.append(d)\n    return out\n"""\n')
BASE_TARGET = 'def api_list():\n    conn = _db()\n    rows = conn.execute("SELECT id, created_at FROM voicemails").fetchall()\n    conn.close()\n    return [dict(r) for r in rows]\n'
WIN = R.refimpl_windows(REF, [BASE_TARGET])
NEWCODE = ('def api_list():\n    rows = conn.execute("SELECT id, created_at, ov_vm, ov_shot FROM voicemails").fetchall()\n'
           '    out = []\n    for r in rows:\n        d = {**dict(r), "vm": bool(r["ov_vm"]), "shot": bool(r["ov_shot"])}\n'
           '        d["screenshot_paths"] = json.loads(r["screenshot_paths"] or "[]")\n        out.append(d)\n    return out\n')
check("psab2: refimpl windows exclude passages the base target already carries (reading the target is not taint)",
      WIN and not (R._windows(R._norm_lines(BASE_TARGET)) & WIN) and (R._windows(R._norm_lines(NEWCODE)) & WIN), str(len(WIN)))
TASK = {"role": "user", "content": "# TASK: demo"}
def asst(name, args):
    return {"role": "assistant", "content": "", "tool_calls": str([{"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}])}
ts = R.taint_scan([TASK, asst("run_bash", {"command": "git show HEAD~1"}), {"role": "tool", "content": "diff\n" + NEWCODE}], WIN)
check("psab2: a refimpl passage in a tool result the model never wrote = TAINTED", ts["tainted"], str(ts))
ts = R.taint_scan([TASK, asst("run_bash", {"command": "cat refimpl.py"}), {"role": "tool", "content": REF.splitlines()[0]}], WIN)
check("psab2: a refimpl signature line in a result = TAINTED", ts["tainted"] and "signature" in ts["why"], str(ts))
ts = R.taint_scan([TASK, asst("edit_file", {"path": "server.py", "new": NEWCODE}), {"role": "tool", "content": "ok"},
                   asst("run_bash", {"command": "cat server.py"}), {"role": "user", "content": "[tool result for run_bash]: " + NEWCODE}], WIN)
check("psab2: the model's OWN edit echoed back (identical to refimpl) is NOT taint", not ts["tainted"], str(ts))
ts = R.taint_scan([TASK, asst("run_bash", {"command": "python3 refimpl.py"}),
                   {"role": "tool", "content": "REFUSED: refimpl.py is not one of your inputs in this task"},
                   {"role": "tool", "content": "[PROTECT_HARNESS: 4 line(s) of the sealed reference solution withheld -- it is not one of your inputs]"}], WIN)
check("psab2: refused / withheld attempts are counted, never taint", not ts["tainted"] and ts["refused"] == 2, str(ts))
ts = R.taint_scan([TASK, asst("read_file", {"path": "server.py"}), {"role": "tool", "content": BASE_TARGET}], WIN)
check("psab2: reading the ORIGINAL target (quoted in refimpl's OLD block) is NOT taint", not ts["tainted"], str(ts))
ts = R.taint_scan([{"role": "user", "content": NEWCODE}], WIN)
check("psab2: the task text itself is not a tool result", not ts["tainted"], str(ts))

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    repo = td / "repo"; repo.mkdir()
    for c in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *c], check=True)
    (repo / "refimpl.py").write_text(REF); (repo / "server.py").write_text(BASE_TARGET)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seal dispatch harness (clean launch baseline)"], check=True)
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    TK = "d" * 12
    (td / "man.json").write_text(json.dumps({"tasks": [{"id": TK, "repo": str(repo), "base_ref": base}]}))
    LW = R.load_refimpl_windows(str(td / "man.json"))
    check("psab2: load_refimpl_windows reads the sealed base via git and drops the target's own passages",
          LW.get(TK) and LW[TK] == R.refimpl_windows(REF, [BASE_TARGET]), str(LW))
    ARMED = ("[worker] WORKER FLAGS %s (from the task's Worker-flags line).\n[worker] PROTECT_HARNESS: sealed coding harness detected -- x\n"
             "[worker] model profile: q\n[worker] CHECKPOINT TRANSCRIPT: %s\n")
    rows = []
    def mk2(name, flags, msgs, passed=True, armed=True):
        tp = td / (name + ".json")
        tp.write_text(json.dumps({"messages": msgs}))
        txt = ARMED % (flags, tp)
        if not armed:
            txt = txt.replace("PROTECT_HARNESS: sealed coding harness detected", "PROTECT_HARNESS requested but this is not a sealed coding job")
        (td / name).write_text(txt)
        rows.append({"transcript_path": str(tp), "iterations": 6, "verify_passed": passed, "max_iters": 20})
    clean = [TASK, asst("edit_file", {"new": NEWCODE}), {"role": "tool", "content": NEWCODE}]
    leak = [TASK, asst("run_bash", {"command": "git log -p"}), {"role": "tool", "content": NEWCODE}]
    mk2(f"111111111111-psab2-A1-{TK}.log", "PROTECT_HARNESS=1", clean)
    mk2(f"222222222222-psab2-B1-{TK}.log", "PROTECT_HARNESS=1 PROSE_STICKY_NOTHINK=1", leak)
    mk2(f"333333333333-psab2-C1-{TK}.log", "PROTECT_HARNESS=1 NOTHINK_RUN=1", clean, armed=False)
    mk2(f"444444444444-psab2-A2-{TK}.log", "NOTHINK_RUN=1", clean)
    mk2(f"555555555555-psab2-A1-{TK}__auto-fix_r1_.log", "PROTECT_HARNESS=1", clean, passed=False)   # a child must never replace A1
    mk2(f"666666666666-psab-A1-{TK}.log", "", clean)
    (td / "m.jsonl").write_text("\n".join(json.dumps(x) for x in rows) + "\n")
    pr, _d = R.collect_psab2(str(td), str(td / "m.jsonl"), str(td / "man.json"))
    check("psab2: collect reads only psab2 first attempts (no child, no v1 psab- log)", sorted(pr) == [("A", 1, TK), ("A", 2, TK), ("B", 1, TK), ("C", 1, TK)], str(sorted(pr)))
    check("psab2: clean armed run is valid and passes", pr[("A", 1, TK)]["valid"] and pr[("A", 1, TK)]["passed"], str(pr[("A", 1, TK)]))
    check("psab2: leaked run is TAINTED, excluded, not a pass", pr[("B", 1, TK)]["tainted"] and not pr[("B", 1, TK)]["valid"] and not pr[("B", 1, TK)]["passed"], str(pr[("B", 1, TK)]))
    check("psab2: a run where PROTECT_HARNESS did not arm is INVALID", not pr[("C", 1, TK)]["valid"] and "did not arm" in pr[("C", 1, TK)]["invalid_why"], str(pr[("C", 1, TK)]))
    check("psab2: arm A without PROTECT_HARNESS=1 / with a foreign arm flag is INVALID", not pr[("A", 2, TK)]["valid"], str(pr[("A", 2, TK)]))
    pr_unk, _ = R.collect_psab2(str(td), str(td / "m.jsonl"), str(td / "man.json"), windows={TK: None})
    check("psab2: unreadable refimpl (taint unknown) -> INVALID, never silently clean", not pr_unk[("A", 1, TK)]["valid"], str(pr_unk[("A", 1, TK)]))
    sm = R.summarize(pr)
    ver = R.verdict_psab2(sm, R.per_task(pr), pr)
    check("psab2: any tainted run -> GUARD-BREACH for both arms", ver["B"]["verdict"] == ver["C"]["verdict"] == "GUARD-BREACH", str(ver))
    ver0 = R.verdict_psab2(sm, R.per_task(pr), {k: v for k, v in pr.items() if not v.get("tainted")})
    check("psab2: no taint -> the v1 rule's verdict (INCONCLUSIVE at this n)", ver0["B"]["verdict"] == "INCONCLUSIVE", str(ver0))
    out = subprocess.run([sys.executable, str(SRC), "--psab2", "--json", "--no-queue", "--logdir", str(td), "--metrics", str(td / "m.jsonl"),
                          "--psab2-manifest", str(td / "man.json")], capture_output=True, text=True)
    jj = json.loads(out.stdout) if out.returncode == 0 else {}
    check("psab2: main --psab2 --json: extras carry Wilson CI + taint count", jj.get("extras", {}).get("B", {}).get("tainted") == 1
          and jj["extras"]["A"]["pass_ci95"][1] == 1.0, out.stderr[-300:] + out.stdout[:300])
    out = subprocess.run([sys.executable, str(SRC), "--psab2", "--no-queue", "--logdir", str(td), "--metrics", str(td / "m.jsonl"),
                          "--psab2-manifest", str(td / "man.json")], capture_output=True, text=True)
    check("psab2: table names the bundle, the Wilson intervals and the tainted run", "prose-sticky-ab2-20261014" in out.stdout
          and "Wilson 95%" in out.stdout and "TAINTED runs" in out.stdout and "B1-" + TK in out.stdout and "GUARD-BREACH" in out.stdout, out.stdout[-900:])
    r_v1, _ = R.collect(str(td), str(td / "m.jsonl"))
    check("psab2 logs never leak into the v1 psab collect()", sorted(r_v1) == [("A", 1, TK)], str(sorted(r_v1)))

MUTANTS = [
    ('s["prose_aborts"] < a["prose_aborts"]', 's["prose_aborts"] <= a["prose_aborts"]', "aborts need not be strictly fewer"),
    ('s["pass_rate"] >= a["pass_rate"]', 's["pass_rate"] > a["pass_rate"]', "pass rate must strictly beat control"),
    ('s["cap_cuts"] <= a["cap_cuts"]', 'True', "cap cut-off check dropped"),
    ('WALL_FACTOR = 1.25', 'WALL_FACTOR = 3.0', "wall slowdown limit loosened"),
    (': wins >= losses,', ': True,', "per-task wins/losses check dropped"),
    ('if a["prose_aborts"] == 0:', 'if False:', "NO-SIGNAL guard dropped"),
    ('MIN_VALID = 12 ', 'MIN_VALID = 1 ', "inconclusive threshold dropped"),
    ('if want and flag_ok and (flag_set & ab_flags) != {want}:\n        flag_ok = False', 'pass', "cross-arm flag leak allowed"),
    ('valid = bool(m) and', 'valid = True or', "runs with no metrics counted as valid"),
    ('-([0-9a-f]{12})\\.log$")', '-([0-9a-f]{12})[^/]*\\.log$")', "auto-fix / lane-change child logs counted as first attempts (arm C inflation)"),
    ('            if sig in body:', '            if False:', "psab2: refimpl signature lines not treated as taint"),
    ('            fresh = sorted(hit - own_w)', '            fresh = []', "psab2: refimpl passages in tool results not treated as taint"),
    ('            fresh = sorted(hit - own_w)', '            fresh = sorted(hit)', "psab2: the model's own echoed code counted as taint"),
    ('        w -= _windows(_norm_lines(bt))', '        pass', "psab2: reading the base target counted as taint"),
    ('    if PH_NOT_ARMED in text or PH_ARMED not in text:', '    if False:', "psab2: runs where PROTECT_HARNESS did not arm counted"),
    ('    elif windows is None:', '    elif False:', "psab2: unknown taint counted as clean"),
    ('    if tainted:\n        for x in ver:', '    if False:\n        for x in ver:', "psab2: GUARD-BREACH dropped"),
    ('def wilson(k, n, z=1.96):', 'def wilson(k, n, z=1.0):', "psab2: Wilson interval not 95%"),
    ('_PSAB2_FNAME = re.compile(r"^([0-9a-f]{12})-psab2-([ABC])(\\d+)-([0-9a-f]{12})\\.log$")',
     '_PSAB2_FNAME = re.compile(r"^([0-9a-f]{12})-psab2-([ABC])(\\d+)-([0-9a-f]{12})[^/]*\\.log$")', "psab2: child logs counted as first attempts"),
]
if "--revert-check" in sys.argv:
    src = SRC.read_text(); bad = 0
    for old, new, why in MUTANTS:
        if old not in src:
            print(f"MUTANT-STALE {why}: anchor missing"); bad += 1; continue
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(src.replace(old, new, 1)); mp = f.name
        r2 = subprocess.run([sys.executable, __file__], env={**os.environ, "REPORT_SRC": mp}, capture_output=True, text=True)
        os.unlink(mp)
        killed = r2.returncode != 0
        print(("RED   " if killed else "GREEN (mutant survived!) ") + why); bad += 0 if killed else 1
    print("REVERT-CHECK: %s" % ("all %d mutants killed" % len(MUTANTS) if not bad else f"{bad} problem(s)"))
    sys.exit(1 if bad else 0)
print("\nPASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
