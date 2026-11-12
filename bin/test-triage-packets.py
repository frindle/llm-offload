#!/usr/bin/env python3
"""Behavioural + REVERT tests for triage_packets / triage-emit / triage-panel-hook.
`test-triage-packets.py` runs the suite on the real ~/bin, then on one mutant copy per guarded
behaviour (each must FAIL). `--suite BINDIR` runs just the suite against BINDIR."""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = ["failure_ledger.py", "triage_packets.py", "triage-emit.py", "triage-panel-hook.sh"]


def row(jid, sig, ts, slice_="s1", **kw):
    r = {"job_id": jid, "signature": sig, "ts": ts, "plan": "p", "bundle": "p", "slice": slice_,
         "label": "auto-author-p-" + slice_, "reason": "nonconvergence", "class": "model",
         "failed_cases": ["case A"], "verify_tail": ["tail line"], "fingerprint": {"tool_counts": {"read_file": 3}},
         "paths": {"livelog": "/nonexistent/x.livelog"}, "round": "author"}
    r.update(kw)
    return r


def suite(bindir):
    bindir = Path(bindir)
    tmp = Path(tempfile.mkdtemp())
    led = tmp / "failures.jsonl"
    env = dict(os.environ, FAILURE_LEDGER=str(led), FAILURE_LEDGER_STATE=str(tmp / "nostate.json"),
               FAILURE_LEDGER_QUEUE_LOGS=str(tmp / "ql"), FAILURE_LEDGER_LIVELOGS=str(tmp / "ll"),
               FAILURE_LEDGER_BIN=str(tmp), TRIAGE_PLANS=str(tmp / "plans"), TRIAGE_WORKTREES=str(tmp / "wt"),
               HOME=str(tmp))
    for k in ("TRIAGE_DIR", "CLAUDE_HEADLESS_DIAGNOSIS"):
        env.pop(k, None)
    (tmp / "bin").symlink_to(bindir)
    fails = []

    def check(name, ok):
        print(("ok   - " if ok else "FAIL - ") + name)
        if not ok:
            fails.append(name)

    def write(rows):
        led.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def py(code):
        p = subprocess.run([sys.executable, "-c", "import sys,json;sys.path.insert(0,%r)\n%s" % (str(bindir), code)],
                           capture_output=True, text=True, env=env, timeout=60)
        return p.stdout.strip()

    def emit(*a):
        return subprocess.run([sys.executable, str(bindir / "triage-emit.py"), *a], capture_output=True,
                              text=True, env=env, timeout=60)

    R = "import triage_packets as t,json;"
    write([row("a1", "read-loop:x", "2026-10-06T10:00:00Z"), row("a2", "read-loop:x", "2026-10-06T11:00:00Z", same_as="a1"),
           row("b1", "literal-cap", "2026-10-06T12:00:00Z", slice_="s2"),
           row("c1", "operator-stop", "2026-10-06T12:30:00Z"), row("d1", "gone", "2026-10-06T09:00:00Z"),
           {"event": "superseded", "job_id": "d1", "ts": "2026-10-06T09:30:00Z"}])
    o = json.loads(py(R + "print(json.dumps(t.refresh()))"))
    check("one packet per distinct signature (2), not per job", sorted(o["opened"]) == ["literal-cap", "read-loop-x"])
    d = tmp / "triage"
    check("operator-stop and fully-superseded signatures get no packet",
          not (d / "operator-stop.md").exists() and not (d / "gone.md").exists())
    pk = (d / "read-loop-x.md").read_text()
    check("packet has repeat chain, FAILED CASE, verify tail, commands and the fix standard",
          "a2 -> a1" in pk and "FAILED CASE: case A" in pk and "tail line" in pk
          and "qctl failures --job a2" in pk and "REQUIRED FIX STANDARD" in pk and "revert test" in pk)
    o2 = json.loads(py(R + "print(json.dumps(t.refresh()))"))
    check("dedupe: open signature is not re-emitted", o2["opened"] == [] and o2["reopened"] == [])
    items = json.loads(emit("--json").stdout)
    check("--json lists repeats first", [i["id"] for i in items["open"]] == ["read-loop-x", "literal-cap"]
          and items["summary"]["open"] == 2 and items["summary"]["repeats"] == 1)
    r = emit("--acted", "read-loop-x", "--outcome", "FIXED", "--reason", "fixed it")
    check("FIXED without commit/job/ref is rejected", r.returncode != 0)
    r = emit("--acted", "nope", "--outcome", "WONTFIX", "--reason", "x")
    check("unknown id rejected", r.returncode != 0)
    r = emit("--acted", "read-loop-x", "--outcome", "FIXED", "--reason", "root cause: plan literal", "--commit", "abc123")
    check("acted accepted", r.returncode == 0)
    check("acted drops out of the open list", [i["id"] for i in json.loads(emit("--json").stdout)["open"]] == ["literal-cap"])
    check("acted + no new rows stays acted", json.loads(py(R + "print(json.dumps(t.refresh()))"))["reopened"] == [])
    write([json.loads(l) for l in led.read_text().splitlines()] + [row("a3", "read-loop:x", "2026-10-06T15:00:00Z", same_as="a2")])
    o3 = json.loads(py(R + "print(json.dumps(t.refresh()))"))
    check("new row after acted reopens the signature", o3["reopened"] == ["read-loop-x"])
    pk = (d / "read-loop-x.md").read_text()
    check("reopened packet carries what was already tried", "What was already tried" in pk
          and "root cause: plan literal" in pk and "abc123" in pk)
    # superseded -> closed
    write([json.loads(l) for l in led.read_text().splitlines()] + [{"event": "superseded", "job_id": "b1", "ts": "2026-10-06T16:00:00Z"}])
    o4 = json.loads(py(R + "print(json.dumps(t.refresh()))"))
    check("open signature whose jobs are all superseded is closed", "literal-cap" in o4["closed"])
    # fail-open
    out = py("import os;os.environ['TRIAGE_DIR']='/proc/nope/x'\n" + R + "print(json.dumps(t.refresh()))")
    check("unwritable triage dir never raises (fail-open)", out.startswith("{"))
    # panel hook
    hook = bindir / "triage-panel-hook.sh"
    p = subprocess.run(["bash", str(hook)], capture_output=True, text=True, env=env, timeout=60)
    ctx = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"] if p.stdout.strip() else ""
    check("hook prints 'N failure signatures need triage' with the command", "1 failure signature(s) need triage" in ctx
          and "triage-emit.py --json" in ctx)
    p = subprocess.run(["bash", str(hook)], capture_output=True, text=True, env=dict(env, CLAUDE_HEADLESS_DIAGNOSIS="1"), timeout=60)
    check("hook is silent in headless diagnosis sessions", p.stdout.strip() == "")
    emit("--acted", "read-loop-x", "--outcome", "ESCALATED", "--reason", "needs the owner")
    p = subprocess.run(["bash", str(hook)], capture_output=True, text=True, env=env, timeout=60)
    check("hook is silent when nothing is open", p.stdout.strip() == "")
    # ledger sweep drives the refresh
    write([row("z1", "newsig", "2026-10-06T17:00:00Z")])
    shutil.rmtree(d)
    py("import failure_ledger as f;f.sweep()")
    check("failure_ledger.sweep() refreshes packets", (d / "newsig.md").exists())
    # one signature seen from a planless job (plan=None) AND a slice job must still get a packet
    # (sorted() over (None, None) vs ('p','s1') keys raised TypeError and refresh() silently dropped it)
    write([row("m1", "mixed-plan", "2026-10-06T18:00:00Z", plan=None, slice=None),
           row("m2", "mixed-plan", "2026-10-06T18:05:00Z")])
    shutil.rmtree(d, ignore_errors=True)
    py("import failure_ledger as f;f.sweep()")
    check("mixed plan=None / plan=str rows still produce a packet", (d / "mixed-plan.md").exists())
    shutil.rmtree(tmp, ignore_errors=True)
    return fails


MUTANTS = {
    "dedupe": ("elif e[\"status\"] == \"open\" and set(ids) != set(e.get(\"job_ids\") or []):",
               "elif e[\"status\"] == \"open\":\n                        out['opened'].append(e['slug'])\n                    elif False:", "triage_packets.py"),
    "reopen": ("if e[\"status\"] in (\"acted\", \"superseded\") and new_live:", "if False:", "triage_packets.py"),
    "history": ("if entry.get(\"history\"):", "if False:", "triage_packets.py"),
    "fixed-needs-ref": ("if outcome == \"FIXED\" and not (commit or job or ref):", "if False:", "triage_packets.py"),
    "skip-operator-stop": ("SKIP = (\"operator-stop\", \"cancelled\")", "SKIP = ()", "triage_packets.py"),
    "sweep-wiring": ("triage_packets.refresh()", "pass", "failure_ledger.py"),
    "headless-skip": ("if [ -n \"${CLAUDE_HEADLESS_DIAGNOSIS:-}\" ]; then exit 0; fi", "", "triage-panel-hook.sh"),
    "mixed-plan-sort": ('key=lambda kv: (str(kv[0][0] or \"\"), str(kv[0][1] or \"\"))', "key=None", "triage_packets.py"),
    "fix-standard": ("REQUIRED FIX STANDARD (non-negotiable)", "", "triage_packets.py"),
}


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--suite":
        return 1 if suite(sys.argv[2]) else 0
    bad = suite(HERE)
    if bad:
        print("REAL FILES FAILED:", bad)
        return 1
    survived = []
    for name, (old, new, fname) in MUTANTS.items():
        t = Path(tempfile.mkdtemp())
        for f in FILES:
            shutil.copy(HERE / f, t / f)
        s = (t / fname).read_text()
        if old not in s:
            print("MUTANT %s: anchor missing" % name)
            survived.append(name)
            continue
        (t / fname).write_text(s.replace(old, new, 1))
        p = subprocess.run([sys.executable, __file__, "--suite", str(t)], capture_output=True, text=True)
        killed = p.returncode != 0
        print("revert %-18s %s" % (name, "killed" if killed else "SURVIVED"))
        if not killed:
            survived.append(name)
        shutil.rmtree(t, ignore_errors=True)
    print("ALL PASS" if not survived else "SURVIVORS: %s" % survived)
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
