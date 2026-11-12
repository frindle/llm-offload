#!/usr/bin/env python3
"""Tests for ~/bin/plan-offload: unit parsing, routing (must-not-offload, leads, names, waits), submit (dry run, a fake
mini-auto, the ledger, resubmits), status, the PostToolUse(ExitPlanMode) hook and install-hook.

Temp git repos and a fake mini-auto only: nothing is queued, and no real settings file, ledger or run dir is touched
(every path plan-offload writes is redirected into a temp dir). Run it on claude-sandbox: python3 test-plan-offload.py
"""
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(os.path.realpath(__file__)).parent
PO = HERE / "plan-offload"
FAILS, COUNT = [], [0]


def check(name, cond, detail=""):
    COUNT[0] += 1
    if not cond:
        FAILS.append(name)
        print(f"FAIL {name}" + (f"\n     {str(detail)[:1500]}" if detail else ""))


T = Path(tempfile.mkdtemp(prefix="po-test-"))
FAKE_LOG = T / "fake-calls.jsonl"
ENV = {k: v for k, v in os.environ.items() if k not in ("PLAN_OFFLOAD", "MINI_AUTO", "MINI_AUTO_CMD", "MINI_RUNNER",
                                                        "MINI_SUBMIT_LIB", "MINI_AUTO_LIB")}
ENV.update(MINI_RUNS=str(T / "runs"), MINI_WT_ROOT=str(T / "wt"), OFFLOAD_HOME=str(T / "home"),
           OFFLOAD_QUEUE_LOGS=str(T / "qlogs"), PLAN_OFFLOAD_SETTINGS=str(T / "settings.json"),
           MINI_AUTO_CMD=f"{sys.executable} {T / 'fake-mini-auto.py'}", FAKE_LOG=str(FAKE_LOG),
           GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
           GIT_COMMITTER_EMAIL="t@example.invalid", GIT_CONFIG_NOSYSTEM="1")
LEDGER = T / "home" / "plan-offload"

(T / "fake-mini-auto.py").write_text(r'''
import hashlib, json, os, sys
from pathlib import Path
a = sys.argv[1:]
desc = sys.stdin.read()
name = a[a.index("--name") + 1]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps({"argv": a, "desc": desc}) + "\n")
if "explode" in name:
    print("mini-submit: FAIL C5 the suite does not run at baseline")
    sys.exit(1)
run = Path(os.environ["MINI_RUNS"]) / name
run.mkdir(parents=True)
print(f"run_dir: {run}\nspec: {run}/spec.json\nbaseline: /wt/{name}\nmini-submit: OK receipt r\n"
      f"job_id: {hashlib.md5(name.encode()).hexdigest()[:12]}")
''')


def sh(*a, cwd=None, inp=None, env=None):
    return subprocess.run([str(x) for x in a], cwd=cwd, input=inp, capture_output=True, text=True, env=env or ENV, timeout=180)


def po(*a, inp=None, env=None):
    t0 = time.time()
    p = sh(sys.executable, PO, *a, inp=inp, env=env)
    p.secs = time.time() - t0
    p.all = p.stdout + p.stderr
    return p


def mkrepo(name, files):
    r = T / "repos" / name
    r.mkdir(parents=True)
    sh("git", "init", "-q", "-b", "main", cwd=r)
    for f, body in files.items():
        (r / f).parent.mkdir(parents=True, exist_ok=True)
        (r / f).write_text(body)
    sh("git", "add", "-A", cwd=r)
    p = sh("git", "commit", "-q", "-m", "init", cwd=r)
    assert p.returncode == 0, p.stderr
    return r.resolve()


def calls():
    return [json.loads(x) for x in FAKE_LOG.read_text().splitlines()] if FAKE_LOG.exists() else []


def ledger(stem):
    p = LEDGER / f"{stem}.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def line_of(out, unit):
    """The routing line of a unit (with its warn: lines)."""
    lines = out.splitlines()
    for i, ln in enumerate(lines):
        if ln[8:].startswith(unit + "  ") or ln[8:] == unit:
            j = i + 1
            while j < len(lines) and lines[j].startswith("        warn:"):
                j += 1
            return "\n".join(lines[i:j])
    return ""


W = mkrepo("widget", {"src/totals.py": "def round_total(x):\n    return round(x)\n",
                      "src/report.py": "def header():\n    return 'h'\n",
                      "lib/report.py": "def other():\n    return 1\n",
                      "src/payment_util.py": "def fee(x):\n    return x\n",
                      "tests/test_totals.py": "def test_x():\n    pass\n"})
B = mkrepo("billing-tools", {"src/calc.py": "def calc():\n    return 1\n"})
DESC = ("round_total(2.5) returns 2 now (banker's rounding); it should round half up. "
        "Example: round_total(2.5) -> 3, round_total(-2.5) -> -3.")


def unit(name, repo=W, file=None, fn=None, desc=DESC, route=None, base=None):
    out = [f"unit: {name}", f"repo: {repo}"]
    if base:
        out.append(f"base: {base}")
    for f in ([file] if isinstance(file, str) else file or []):
        out.append(f"file: {f}")
    if fn:
        out.append(f"fn: {fn}")
    if route:
        out.append(f"route: {route}")
    out.append(f"desc: {desc}")
    return "\n".join(out) + "\n"


def plan_file(name, units_text, prose="Change src/totals.py so totals round half up.\n"):
    p = T / "plans" / f"{name}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"# Plan {name}\n\n{prose}\n```offload\n{units_text}```\n\nThen run the tests.\n")
    return p


# ---------------------------------------------------------------------------------------------------- parsing (module)
os.environ.update(ENV)
ld = SourceFileLoader("plan_offload_t", str(PO))
M = importlib.util.module_from_spec(importlib.util.spec_from_loader("plan_offload_t", ld))
ld.exec_module(M)

md = ("x\n```offload\nunit: aaa-bbb\n```\n```python\nunit: not-this\n```\n  ~~~offload\nunit: ccc-ddd\n  ~~~\n"
      "```offload\n```\n")
body = M.fences(md)
check("fences: offload bodies only", "aaa-bbb" in body and "ccc-ddd" in body and "not-this" not in body, body)

us, es = M.parse_units("unit: aaa-bbb\nrepo: /x\ndesc: first line\n  second line\n    file: not-a-key\nthird\n\nfourth\n"
                       "unit: eee-fff\n- repo: /y\n* file: a.py, b.py\nfile: `c.py`\n# a comment\nfn: go\n")
check("parse: two units", [u["unit"] for u in us] == ["aaa-bbb", "eee-fff"] and not es, (us, es))
check("parse: desc continuation, indented key word stays text",
      us[0]["desc"] == "first line\nsecond line\nfile: not-a-key\nthird\n\nfourth" and us[0]["file"] == [], us[0])
check("parse: bullets, comma lists, backticks, comments", us[1]["repo"] == "/y" and us[1]["file"] == ["a.py", "b.py", "c.py"]
      and us[1]["fn"] == "go", us[1])
_, es = M.parse_units("repo: /x\nunit: aaa-bbb\nrepo: /y\nrepo: /z\nhello there\n")
check("parse: key before unit, second key, junk line",
      len(es) == 3 and "before the first" in es[0] and "second `repo:`" in es[1] and "not a `key: value`" in es[2], es)
check("resolve_lead: exact, suffix, ambiguous, line suffix",
      M.resolve_lead("src/a.py", ["src/a.py"]) == ("src/a.py", []) and M.resolve_lead("./a.py:12", ["src/a.py"]) == ("src/a.py", [])
      and M.resolve_lead("a.py", ["x/a.py", "y/a.py"]) == (None, ["x/a.py", "y/a.py"]))

# ---------------------------------------------------------------------------------------------------- routing (check)
units = (unit("totals-round-half-up", file="totals.py", fn="round_total")
         + unit("login-redirect-target", file="src/report.py", desc="After login the redirect drops the next= target; it "
                "should keep it. Example: /login?next=/a -> /a")
         + unit("fee-calc-rounding", file="src/payment_util.py", desc="fee(10) returns 10 now; it should add the 2% surcharge, "
                "so fee(10) -> 10.2.")
         + unit("calc-result-value", repo=B, file="src/calc.py", desc="calc() returns 1 now; it should return 2 when called "
                "without arguments, e.g. calc() -> 2.")
         + unit("header-missing-space", file="src/nope.py")
         + unit("report-header-text", file="report.py")
         + unit("fix-it", file="src/report.py")
         + unit("totals-round-half-up", file="src/report.py")
         + unit("report-short-desc", file="src/report.py", desc="make it better")
         + unit("report-wiring-change", file="src/report.py", route="claude - wiring across two files")
         + unit("report-route-bogus", file="src/report.py", route="sometimes"))
p = po("check", "-", inp=units)
o = p.stdout
check("check: exit 1 with errors", p.returncode == 1, p.all)
check("check: good unit -> lane with the resolved lead",
      line_of(o, "totals-round-half-up").startswith(f"lane    totals-round-half-up  {W}@HEAD  file=src/totals.py fn=round_total"), o)
check("check: forbidden description -> claude", line_of(o, "login-redirect-target").startswith("claude ")
      and "must-not-offload" in line_of(o, "login-redirect-target") and "login" in line_of(o, "login-redirect-target"), o)
check("check: forbidden file -> claude", line_of(o, "fee-calc-rounding").startswith("claude ")
      and "payment" in line_of(o, "fee-calc-rounding"), o)
check("check: forbidden repo -> claude", line_of(o, "calc-result-value").startswith("claude ")
      and "billing" in line_of(o, "calc-result-value"), o)
check("check: untracked lead -> error", line_of(o, "header-missing-space").startswith("ERROR ")
      and "not a tracked file" in line_of(o, "header-missing-space"), o)
check("check: ambiguous lead -> error naming both", "matches 2 tracked files" in line_of(o, "report-header-text")
      and "lib/report.py" in line_of(o, "report-header-text"), o)
check("check: bad label -> error", line_of(o, "fix-it").startswith("ERROR ") and "meaningful token" in line_of(o, "fix-it"), o)
check("check: duplicate unit name -> error", "used twice" in o, o)
check("check: short desc -> error", "desc is 14 chars" in line_of(o, "report-short-desc"), o)
check("check: route claude with a reason", line_of(o, "report-wiring-change").startswith(
      "claude  report-wiring-change  wiring across two files"), o)
check("check: unknown route -> error", "route 'sometimes'" in line_of(o, "report-route-bogus"), o)
check("check: summary line", "1 lane, 4 claude, 0 later, 6 with errors" in o, o)

p = po("check", "-", inp=unit("totals-round-half-up", file="src/totals.py") + unit("totals-negative-zero", file="src/totals.py")
       + unit("totals-doc-wording", file="src/totals.py", route="claude - docstring only"))
o = p.stdout
check("check: same file twice -> the second waits", p.returncode == 0 and line_of(o, "totals-negative-zero").startswith(
      "later   totals-negative-zero  waits for lane unit totals-round-half-up"), o)
check("check: claude unit on a lane unit's file -> warned", "lane unit totals-round-half-up changes src/totals.py too"
      in line_of(o, "totals-doc-wording"), o)
check("check: summary counts waits", "1 lane, 1 claude, 1 later, 0 with errors; submit:" in o, o)

p = po("check", "-", inp=unit("totals-round-half-up", file="src/totals.py"), env={**ENV, "MINI_AUTO": "off"})
check("check: MINI_AUTO=off -> claude", "the lane is off" in line_of(p.stdout, "totals-round-half-up"), p.all)

p = po("check", "-", inp=unit("totals-round-half-up", file="src/totals.py", base="nosuchref"))
check("check: base that is no commit -> error", p.returncode == 1 and "base 'nosuchref' is not a commit" in p.stdout, p.all)
sh("git", "remote", "add", "origin", T / "no-such-remote", cwd=W)
p = po("check", "-", inp=unit("totals-round-half-up", file="src/totals.py", base="origin/main"))
check("check: unfetched remote base -> warning only", p.returncode == 0 and "is not fetched here yet" in p.stdout, p.all)
sh("git", "remote", "remove", "origin", cwd=W)

(W / "src/totals.py").write_text("def round_total(x):\n    return round(x)  # local edit\n")
p = po("check", "-", inp=unit("totals-round-half-up", file="src/totals.py"))
check("check: uncommitted edit to a lead -> warning", p.returncode == 0 and "the checkout's src/totals.py differs from HEAD's"
      in p.stdout, p.all)
sh("git", "checkout", "--", "src/totals.py", cwd=W)
p = po("check", "-", inp="unit: aaa-bbb\nnonsense line\n")
check("check: parse error -> exit 1", p.returncode == 1 and "not a `key: value`" in p.stdout, p.all)
p = po("check", "-", inp="")
check("check: no units -> exit 1", p.returncode == 1 and "no units" in p.stdout, p.all)
p = po("check")
check("check: no source -> exit 2", p.returncode == 2, p.all)

# ---------------------------------------------------------------------------------------------------- submit
P1 = plan_file("alpha-plan", unit("totals-round-half-up", file="src/totals.py", fn="round_total")
               + unit("report-wiring-change", file="src/report.py", route="claude - wiring across two files"))
p = po("submit", "--plan", P1, "--dry-run")
want = (f"--repo {W} --base HEAD --name totals-round-half-up --file src/totals.py --fn round_total --desc-file - --submit "
        f"--label totals-round-half-up")
check("submit --dry-run: prints the command", p.returncode == 0 and "would run:" in p.stdout and want in p.stdout, p.all)
check("submit --dry-run: creates nothing", not calls() and not ledger("alpha-plan") and not (T / "runs").exists(), p.all)

bad = plan_file("bad-plan", unit("totals-round-half-up", file="src/totals.py") + unit("header-missing-space", file="src/nope.py"))
p = po("submit", "--plan", bad)
check("submit: any error -> nothing submitted", p.returncode == 1 and "nothing submitted" in p.all and not calls(), p.all)
p = po("submit", "--plan", bad, "--only", "nope-unit-name")
check("submit: --only unknown -> exit 2", p.returncode == 2, p.all)

p = po("submit", "--plan", P1, "--model", "test-model-x")
c = calls()
job1 = hashlib.md5(b"totals-round-half-up").hexdigest()[:12]
check("submit: one mini-auto call, for the lane unit", p.returncode == 0 and len(c) == 1, p.all)
check("submit: argv and stdin", c and " ".join(c[0]["argv"]).startswith("new " + want) and c[0]["argv"][-2:] == [
      "--model", "test-model-x"] and c[0]["desc"] == DESC + "\n", c)
check("submit: queued line", f"queued: job {job1} (totals-round-half-up)" in p.stdout, p.all)
rows = ledger("alpha-plan")
check("submit: ledger rows", len(rows) == 2 and rows[0]["route"] == "lane" and rows[0]["job"] == job1
      and rows[0]["files"] == ["src/totals.py"] and rows[0]["plan"] == str(P1) and rows[1]["route"] == "claude"
      and rows[1]["reason"] == "wiring across two files", rows)

p = po("submit", "--plan", P1)
check("submit again: skips the queued unit", p.returncode == 0 and "already queued as job " + job1 in p.stdout
      and len(calls()) == 1 and len(ledger("alpha-plan")) == 2, p.all)
p = po("submit", "--plan", P1, "--only", "totals-round-half-up")
c = calls()
check("submit --only: resubmits under a free name", p.returncode == 0 and len(c) == 2
      and "--name totals-round-half-up-r2" in " ".join(c[1]["argv"]) and ledger("alpha-plan")[-1]["name"] == "totals-round-half-up-r2",
      p.all)

p = po("submit", "-", inp=unit("explode-the-widget", file="src/totals.py"))
rows = ledger("adhoc")
check("submit: mini-auto failure -> FAILED, rc 1, ledger keeps rc", p.returncode == 1 and "FAILED  explode-the-widget" in p.stdout
      and "C5 the suite" in p.stdout and rows and rows[-1]["job"] is None and rows[-1]["rc"] == 1, p.all)

P2 = plan_file("beta-plan", unit("totals-round-half-up", file="src/totals.py") + unit("totals-negative-zero", file="src/totals.py"))
p = po("submit", "--plan", P2)
rows = ledger("beta-plan")
check("submit: the second unit on a file waits", p.returncode == 0 and "later   totals-negative-zero" in p.stdout
      and [r["route"] for r in rows] == ["lane", "later"] and rows[1]["after"] == "totals-round-half-up", p.all)
n = len(calls())
p = po("submit", "--plan", P2, "--only", "totals-negative-zero")
check("submit --only the waiting unit: queued, with a pending-overlap warning", p.returncode == 0 and len(calls()) == n + 1
      and "also changes src/totals.py and has not landed" in p.stdout, p.all)

# ---------------------------------------------------------------------------------------------------- status
p = po("status", "--plan", P1)
check("status: queued and claude", p.returncode == 0 and "queued/running" in p.stdout and "yours to write (wiring" in p.stdout,
      p.all)
job2 = hashlib.md5(b"totals-round-half-up-r2").hexdigest()[:12]
(T / "qlogs").mkdir(exist_ok=True)
(T / "qlogs" / f"{job2}.done.json").write_text(json.dumps({"status": "done", "runner_verdict": "PASS",
                                                          "cwd": str(T / "runs" / "totals-round-half-up-r2" / "work")}))
p = po("status", "--plan", P1)
check("status: passed -> review step", f"passed (PASS)" in p.stdout and f"mini-scorecard review brief {job2}" in p.stdout, p.all)
(T / "home" / "reviews.jsonl").write_text(json.dumps({"job": job2, "verdict": "ok"}) + "\n")
p = po("status", "--plan", P1)
check("status: reviewed ok -> commit step", "reviewed ok" in p.stdout and "mini-scorecard ledger add --job " + job2 in p.stdout,
      p.all)
with open(T / "home" / "reviews.jsonl", "a") as fh:
    fh.write(json.dumps({"job": job2, "verdict": "reject", "note": "x"}) + "\n")
p = po("status", "--plan", P1)
check("status: rejected -> yours", "rejected" in p.stdout and "mini-auto show " + job2 in p.stdout, p.all)
jb = hashlib.md5(b"totals-negative-zero").hexdigest()[:12]
(T / "runs" / "totals-negative-zero" / "work").mkdir(parents=True, exist_ok=True)
(T / "qlogs" / "archive").mkdir(exist_ok=True)
(T / "qlogs" / "archive" / f"{jb}.done.json").write_text(json.dumps({"status": "failed", "runner_verdict": "FAIL",
                                                                     "cwd": str(T / "runs" / "totals-negative-zero" / "work")}))
p = po("status", "--plan", P2)
check("status: failed (archived record) -> yours", "failed (failed, FAIL)" in p.stdout, p.all)
(T / "runs" / "totals-negative-zero" / "finish").mkdir()
(T / "runs" / "totals-negative-zero" / "finish" / "FINISHED.json").write_text(json.dumps({"verdict": "PASS"}))
p = po("status", "--plan", P2)
check("status: finished by mini-finish -> land", "finished (mini-finish PASS)" in p.stdout and "mini-auto land " + jb in p.stdout,
      p.all)
p = po("status", "--all")
check("status --all: every ledger", all(s in p.stdout for s in ("== alpha-plan", "== beta-plan", "== adhoc")), p.all)
p = po("status", "--plan", T / "plans" / "never.md")
check("status: nothing yet", "nothing submitted yet for never" in p.stdout, p.all)

# ---------------------------------------------------------------------------------------------------- hook
P3 = plan_file("gamma-plan", unit("totals-round-half-up", file="src/totals.py", fn="round_total")
               + unit("report-wiring-change", file="src/report.py", route="claude - wiring"))
PLAN3 = P3.read_text()


def hook(tool_response, tool_input=None, env=None, **extra):
    d = {"session_id": "s1", "transcript_path": str(T / "transcript.jsonl"), "cwd": str(W), "permission_mode": "default",
         "hook_event_name": "PostToolUse", "tool_name": "ExitPlanMode", "tool_input": tool_input or {},
         "tool_response": tool_response, **extra}
    p = po("hook", inp=json.dumps(d), env=env)
    try:
        ctx = json.loads(p.stdout)["hookSpecificOutput"] if p.stdout.strip() else None
    except ValueError:
        ctx = {"bad": p.stdout}
    return p, ctx


p, ctx = hook({"plan": PLAN3, "isAgent": False, "filePath": str(P3), "hasTaskTool": True, "planWasEdited": False},
              {"plan": PLAN3})
a = (ctx or {}).get("additionalContext", "")
check("hook: units -> PostToolUse additionalContext", p.returncode == 0 and ctx and ctx.get("hookEventName") == "PostToolUse"
      and a.startswith("PLAN APPROVED -> OFFLOAD-FIRST CODING"), p.all)
check("hook: names the units and the submit command", "lane    totals-round-half-up" in a and "claude  report-wiring-change" in a
      and f"~/bin/plan-offload submit --plan {P3}   (queues 1 lane unit(s)" in a, a)
check("hook: under 10 s", p.secs < 10, p.secs)

p, ctx = hook({"plan": "# Plan\n\nRename the helper in src/totals.py and update its callers.\n", "filePath": str(T / "p4.md")})
a = (ctx or {}).get("additionalContext", "")
check("hook: no units but code -> the template", "<<'EOF'\nunit: <area-change>\nrepo: <checkout path>" in a
      and f"submit --plan {T / 'p4.md'} - <<'EOF'" in a and "\nEOF\n" in a and "say so in one line" in a, a)
p, ctx = hook({"plan": "# Plan\n\nReword the onboarding doc and the README intro.\n", "filePath": str(T / "p5.md")})
check("hook: no code -> silent", p.returncode == 0 and ctx is None and not p.stdout.strip(), p.all)
for nm, tr, extra in (("isAgent", {"plan": PLAN3, "isAgent": True}, {}),
                      ("awaitingLeaderApproval", {"plan": PLAN3, "filePath": str(P3), "awaitingLeaderApproval": True}, {}),
                      ("agent_id", {"plan": PLAN3, "filePath": str(P3)}, {"agent_id": "a1"}),
                      ("other tool", {"plan": PLAN3, "filePath": str(P3)}, {"tool_name": "Bash"}),
                      ("empty plan", {"plan": "  ", "filePath": ""}, {})):
    p, ctx = hook(tr, **extra)
    check(f"hook: {nm} -> silent", p.returncode == 0 and ctx is None, p.all)
p, ctx = hook({"plan": PLAN3, "filePath": str(P3)}, env={**ENV, "PLAN_OFFLOAD": "off"})
check("hook: PLAN_OFFLOAD=off -> silent", ctx is None, p.all)
LEDGER.mkdir(parents=True, exist_ok=True)
(LEDGER / "OFF").write_text("")
p, ctx = hook({"plan": PLAN3, "filePath": str(P3)})
check("hook: OFF file -> silent", ctx is None, p.all)
(LEDGER / "OFF").unlink()
p, ctx = hook({"filePath": str(P3)})
check("hook: path only -> reads the plan file", ctx and "lane    totals-round-half-up" in ctx.get("additionalContext", ""), p.all)
p, ctx = hook(json.dumps({"plan": PLAN3, "filePath": str(P3)}))
check("hook: tool_response as a JSON string", ctx and "lane    totals-round-half-up" in ctx.get("additionalContext", ""), p.all)
p, ctx = hook({"plan": PLAN3})
a = (ctx or {}).get("additionalContext", "")
saved = list((LEDGER / "plans").glob("plan-*.md")) if (LEDGER / "plans").exists() else []
check("hook: plan text without a path -> saved copy for --plan", len(saved) == 1 and saved[0].read_text() == PLAN3
      and f"submit --plan {saved[0]}" in a, (saved, a[-300:]))
tp_plan = T / "fakehome" / ".claude" / "plans" / "quiet-river.md"
tp_plan.parent.mkdir(parents=True)
tp_plan.write_text(PLAN3)
(T / "transcript.jsonl").write_text(json.dumps({"message": f"plan saved to {tp_plan}"}) + "\n"
                                    + json.dumps({"message": f"{tp_plan.parent}/quiet-river-agent-a1.md"}) + "\n")
p, ctx = hook({}, {})
check("hook: nothing in the payload -> the transcript's plan file", ctx and f"--plan {tp_plan}" in ctx.get("additionalContext", ""),
      p.all)
p, ctx = hook({"plan": "# x\n```offload\nunit: totals-round-half-up\nrepo: /no/such/dir\ndesc: " + DESC + "\n```\n",
               "filePath": str(T / "p6.md")})
check("hook: unit errors -> fix first", ctx and "First: fix the ERROR lines" in ctx.get("additionalContext", "")
      and "is not a git checkout" in ctx.get("additionalContext", ""), p.all)
p, ctx = hook({"plan": "# x\n```offload\n" + unit("report-wiring-change", file="src/report.py", route="claude - wiring")
               + "```\n", "filePath": str(T / "p7.md")})
check("hook: only claude units -> no lane step", ctx and "No unit goes to the lane now" in ctx.get("additionalContext", ""), p.all)
p = po("hook", inp="{not json")
check("hook: malformed input -> exit 0, silent", p.returncode == 0 and not p.stdout.strip(), p.all)
log = (LEDGER / "hook.log").read_text()
rows = [json.loads(x) for x in log.splitlines()]
check("hook.log: keys and sources, no plan text", rows and "round half up" not in log and any(
      r.get("plan_from") == "tool_response.plan" and "filePath" in r.get("response_keys", []) for r in rows)
      and any(r.get("plan_from") == "file" for r in rows), log[-600:])

# ---------------------------------------------------------------------------------------------------- install-hook
S = T / "settings.json"
orig = {"env": {"SOME_TOKEN": "not-a-real-value"}, "hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "echo hi"}]}], "Stop": []}, "theme": "dark", "note": "naïve ✓"}
otext = json.dumps(orig, indent=2, ensure_ascii=False) + "\n"
S.write_text(otext)
os.chmod(S, 0o600)
p = po("install-hook", "--dry-run")
check("install-hook --dry-run: no change", p.returncode == 0 and "would add" in p.stdout and S.read_text() == otext, p.all)
p = po("install-hook")
new = json.loads(S.read_text())
exp = json.loads(otext)
exp["hooks"]["PostToolUse"].append({"matcher": "ExitPlanMode", "hooks": [{"type": "command",
                                    "command": "python3 ~/bin/plan-offload hook", "timeout": 30}]})
baks = list(T.glob("settings.json.bak-plan-offload-*"))
check("install-hook: adds the PostToolUse entry, keeps the rest", p.returncode == 0 and new == exp, (p.all, new))
check("install-hook: same formatting", S.read_text() == json.dumps(exp, indent=2, ensure_ascii=False) + "\n")
check("install-hook: mode kept", stat.S_IMODE(S.stat().st_mode) == 0o600, oct(S.stat().st_mode))
check("install-hook: backup of the original, same mode", len(baks) == 1 and baks[0].read_text() == otext
      and stat.S_IMODE(baks[0].stat().st_mode) == 0o600, baks)
check("install-hook: no temp file left", not list(T.glob("settings.json.tmp*")))
before = S.read_text()
p = po("install-hook")
check("install-hook again: already installed, no change", p.returncode == 0 and "already installed" in p.stdout
      and S.read_text() == before and len(list(T.glob("settings.json.bak-plan-offload-*"))) == 1, p.all)
S.write_text('{"theme": "dark"}\n')
held = []  # backup names for the next seconds are taken: the install must pick another name, never overwrite one
for i in range(8):
    h = T / ("settings.json.bak-plan-offload-" + time.strftime("%Y%m%dT%H%M%S", time.localtime(time.time() + i)))
    if not h.exists():
        h.write_text("placeholder\n")
        held.append(h)
p = po("install-hook")
check("install-hook: settings without hooks", p.returncode == 0 and json.loads(S.read_text())["hooks"]["PostToolUse"][0][
      "matcher"] == "ExitPlanMode", p.all)
fresh = [b for b in T.glob("settings.json.bak-plan-offload-*") if b.read_text() == '{"theme": "dark"}\n']
check("install-hook: a taken backup name is never overwritten", baks[0].read_text() == otext and len(fresh) == 1
      and all(h.read_text() == "placeholder\n" for h in held), [b.name for b in T.glob("settings.json.bak-*")])
S.write_text("{broken")
p = po("install-hook")
check("install-hook: broken settings -> exit 2, untouched", p.returncode == 2 and S.read_text() == "{broken", p.all)

# ---------------------------------------------------------------------------------------------------- summary
if os.environ.get("KEEP") != "1":
    shutil.rmtree(T, ignore_errors=True)
print(f"{COUNT[0]} checks, {len(FAILS)} failed" + (f": {', '.join(FAILS)}" if FAILS else ""))
sys.exit(1 if FAILS else 0)
