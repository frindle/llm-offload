#!/usr/bin/env python3
"""ladder-log: ledger rows and per-model cost from synthetic subagent transcripts (hand-computed dollars).

Run: python3 test-ladder-log.py [--src PATH]   (run on claude-sandbox, not the Mac)
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

BIN = Path(__file__).resolve().parent
FAILS = []
OK_MARK = "LADDER_LOG_OK"


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else f" -- got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


def line(mid, model, **u):
    return json.dumps({"type": "assistant", "uuid": "u" + mid,
                       "message": {"id": mid, "model": model, "usage": u}})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(BIN / "ladder-log"))
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="ladder-log-"))
    proj = tmp / "proj" / "-p" / "sess" / "subagents"
    proj.mkdir(parents=True)
    env = dict(os.environ, LADDER_LEDGER=str(tmp / "ledger.jsonl"), LADDER_PROJECTS=str(tmp / "proj"))

    def ll(*args):
        return subprocess.run([sys.executable, "-I", a.src, *args], capture_output=True, text=True, env=env)

    # Haiku agent: h1 = 1975 micro-USD (twice in file), h2 over 100k = 8550 micro-USD -> $0.010525
    h1 = line("h1", "claude-haiku-5-5", input_tokens=1000, output_tokens=2000, cache_read_input_tokens=10000,
              cache_creation_input_tokens=5000,
              cache_creation={"ephemeral_5m_input_tokens": 3000, "ephemeral_1h_input_tokens": 2000})
    h2 = line("h2", "claude-haiku-5-5", input_tokens=100, output_tokens=1000, cache_read_input_tokens=120000,
              cache_creation_input_tokens=0)
    (proj / "agent-aH.jsonl").write_text("\n".join([h1, h1, h2, "not json"]) + "\n")
    # Sonnet agent: s1 no cache breakdown (all 5m) = 14500 micro; o1 opus = 2400 micro -> $0.0169
    s1 = line("s1", "claude-sonnet-5-5", input_tokens=1000, output_tokens=1000, cache_creation_input_tokens=1000)
    o1 = line("o1", "claude-opus-5-5", input_tokens=100, output_tokens=100)
    (proj / "agent-aS.jsonl").write_text("\n".join([s1, o1, s1]) + "\n")
    # Unknown model agent
    (proj / "agent-aU.jsonl").write_text(line("x1", "claude-foo-1", input_tokens=500, output_tokens=500) + "\n")

    def add(task, rung, result, agent=None):
        args = ["add", "--task", task, "--repo", "/r", "--rung", rung, "--result", result, "--check", "pytest"]
        if agent:
            args += ["--agent-id", agent]
        return ll(*args)

    r = add("t1-202611090000", "haiku", "fail", "aH")
    check("add rc", r.returncode, 0)
    add("t1-202611090000", "sonnet", "pass", "aS")
    add("t1-202611090000", "review", "pass")
    add("t2-202611090001", "haiku", "pass", "aU")
    add("t3-202611090002", "haiku", "pass", "aMissing")
    rows = [json.loads(x) for x in (tmp / "ledger.jsonl").read_text().splitlines()]
    check("ledger rows", len(rows), 5)
    check("row fields", (rows[0]["task"], rows[0]["rung"], rows[0]["result"], rows[0]["agent_id"], rows[0]["check"]),
          ("t1-202611090000", "haiku", "fail", "aH", "pytest"))
    check("row ts is UTC ISO", rows[0]["ts"].endswith("+00:00"), True)
    check("bad rung rejected", ll("add", "--task", "x", "--repo", "/r", "--rung", "bogus", "--result", "pass").returncode != 0, True)

    out = ll("report").stdout
    print(out)
    check("rungs shown", "rungs haiku > sonnet > review" in out, True)
    check("haiku cost", "haiku agent aH: $0.0105" in out, True)
    check("duplicate id counted once (haiku total)", "claude-haiku-5-5 $0.0105" in out, True)
    check("sonnet agent cost", "sonnet agent aS: $0.0169" in out, True)
    check("task1 cost", "task cost: $0.0274" in out, True)  # 0.010525 + 0.0169 = 0.027425
    check("unknown model warning", "unknown model 'claude-foo-1': 1000 tokens counted, priced 0" in out, True)
    check("missing transcript row", "agent aMissing: transcript missing, cost unknown" in out, True)
    check("first pass rate", "Haiku first-pass rate 67% (2/3)" in out, True)
    check("escalation rate", "escalation to Sonnet 33% (1/3)" in out, True)
    check("total cost", "cost total: $0.0274" in out, True)
    check("opus line", "claude-opus-5-5: $0.0024" in out, True)
    check("review note", "main-session review cost is not in subagent transcripts" in out, True)
    check("since filter empty", "no ledger rows since 2999-01-01" in ll("report", "--since", "2999-01-01").stdout, True)

    if FAILS:
        print("FAILED:", ", ".join(FAILS))
        sys.exit(1)
    print(OK_MARK)


if __name__ == "__main__":
    main()
