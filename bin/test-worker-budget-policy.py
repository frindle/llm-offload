#!/usr/bin/env python3
"""Switchable agent-budget policies (worker_budget.py + its seams in ollama-worker.py), 2026-10-09.

UNIT (pure): policy parsing/precedence, verify normalisation + scoring, the progress_extend decision
table, stale-read stubs, prefetch excerpts, checkpoint-revert bookkeeping, read-streak counting.
INTEGRATION (real run_task, model stubbed, no GPU/network): for EACH policy a script that makes it
fire, plus the DEFAULT-OFF contract (no policy == byte-identical prompts to `--budget-policy none`,
no BUDGET log line, no new metrics keys).

WORKER_SRC / BUDGET_SRC override the files under test (pipeline-canary --prove mutates copies)."""
import importlib.util, json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKER = Path(os.environ.get("WORKER_SRC") or HERE / "ollama-worker.py")
BUDGET = Path(os.environ.get("BUDGET_SRC") or HERE / "worker_budget.py")
FAILS = []


def check(name, got, want=True):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def _load_budget():
    spec = importlib.util.spec_from_file_location("worker_budget", BUDGET)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["worker_budget"] = mod        # the worker does `import worker_budget`
    spec.loader.exec_module(mod)
    return mod


B = _load_budget()


def _load_worker():
    os.environ["HOME"] = tempfile.mkdtemp(prefix="budget-home-")
    os.environ.pop("VAULT_TOKEN_FILE", None)
    os.environ.pop("WORKER_BUDGET_POLICY", None)
    sys.path.insert(0, str(WORKER.parent))
    spec = importlib.util.spec_from_file_location("ow_budget", WORKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.log_dispatch_to_vault = lambda *a, **k: None
    return mod


# ---------------------------------------------------------------------------------------- unit
def unit():
    print("== switch parsing ==")
    check("empty -> nothing", B.parse_policy_list(""), (frozenset(), []))
    check("list + dash + case", B.parse_policy_list("Progress-Extend, budget_visible")[0],
          frozenset({"progress_extend", "budget_visible"}))
    check("all expands", B.parse_policy_list("all")[0], frozenset(B.POLICIES))
    check("none is empty", B.parse_policy_list("none")[0], frozenset())
    check("unknown reported, known kept", B.parse_policy_list("read_window,bogus"), (frozenset({"read_window"}), ["bogus"]))
    check("task marker", B.task_policy_marker("# T\nBudget-policy: read_window, budget_visible\nmore"),
          "read_window, budget_visible")
    check("no marker", B.task_policy_marker("nothing here"), None)
    os.environ["WORKER_BUDGET_POLICY"] = "read_streak_nudge"
    try:
        check("env used when nothing else", B.resolve_policies(None, None, "x")[2:], ("env",))
        check("task marker beats env", B.resolve_policies(None, None, "Budget-policy: read_window")[0],
              frozenset({"read_window"}))
        check("explicit CLI none beats env and marker", B.resolve_policies("none", None, "Budget-policy: read_window")[0],
              frozenset())
    finally:
        os.environ.pop("WORKER_BUDGET_POLICY", None)
    check("default is empty", B.resolve_policies(None, None, "x"), (frozenset(), [], "default"))

    print("== verify helpers ==")
    run = json.dumps({"exit_code": 1, "stdout": "FAIL a\n3 failed in 1.2s", "stderr": "", "cwd": "/x"})
    txt, ok = B.verify_text_from_bash(run)
    check("run_bash json unwrapped", (ok, "3 failed" in txt), (False, True))
    check("timing-only change compares equal", B.normalize_verify("3 failed in 1.2s\nok 10ms"),
          B.normalize_verify("3 failed in 9.9s\nok 77ms"))
    check("real change compares different", B.normalize_verify("3 failed") != B.normalize_verify("2 failed"), True)
    check("passing verify scores 0", B.verify_score("all good", True), 0)
    check("N failed parsed", B.verify_score("x\n4 failed, 9 passed", False), 4)
    check("unscorable failing output is None", B.verify_score("boom", False), None)
    check("sig_fn count used", B.verify_score("a\nb\nc", False, lambda t: set(t.splitlines())), 3)

    print("== progress_extend decision table ==")
    def st(**kw):
        s = B.BudgetState({"progress_extend"}, kw.pop("cap", 14), **kw)
        return s
    s = st()
    check("no write -> refuse", s.try_extend(14, 14), (0, None))
    check("  reason recorded", "no write" in (s.refused or ""), True)
    s = st(); s.write_iters = [13]
    s.try_extend(14, 14)
    check("write but no verify -> refuse", "no verify" in (s.refused or ""), True)
    s = st(cap=24); s.write_iters = [22]; s.verify_tails = [(5, "a"), (21, "b")]
    ex, note = s.try_extend(24, 24)
    check("write in last 3 + changed verify -> +8", ex, 8)
    check("  note names the grant", "+8" in note and "32" in note, True)
    check("  second ask is idempotent", s.try_extend(32, 32), (0, None))
    s = st(cap=24, refine=True); s.write_iters = [24]; s.verify_tails = [(9, "a"), (13, "b")]
    check("refine task gets +12 (24 -> 36 = the 1.5x ceiling exactly)", s.try_extend(24, 24)[0], 12)
    s = st(cap=14); s.write_iters = [14]; s.verify_tails = [(9, "a"), (13, "b")]
    check("cap 14: +8 is clamped to the 1.5x ceiling (21) -> +7", s.try_extend(14, 14)[0], 7)
    s = st(cap=14); s.write_iters = [17]; s.verify_tails = [(9, "a"), (13, "b")]
    check("ceiling clamps against the CURRENT cap (18 -> +3)", s.try_extend(18, 18)[0], 3)
    s = st(cap=14); s.write_iters = [14]; s.verify_tails = [(9, "a"), (13, "b")]
    check("at the ceiling -> refuse", s.try_extend(21, 21), (0, None))
    check("  ceiling reason", "ceiling" in (s.refused or ""), True)
    s = st(); s.write_iters = [14]; s.verify_tails = [(4, "same"), (9, "same"), (13, "same")]
    check("3 identical verify tails -> refuse", s.try_extend(14, 14), (0, None))
    check("  stalled reason", "identical" in (s.refused or ""), True)
    s = st(); s.write_iters = [14]; s.verify_tails = [(9, "x"), (13, "same"), (14, "same")]
    check("last two equal -> refuse", s.try_extend(14, 14)[0], 0)
    s = st(cap=24); s.write_iters = [24]; s.verify_tails = [(13, "only one")]
    check("single verify -> grant (no evidence of stall)", s.try_extend(24, 24)[0], 8)
    s = B.BudgetState({"budget_visible"}, 14)
    check("not enabled -> never extends", s.try_extend(14, 14), (0, None))
    s = B.BudgetState({"budget_visible", "progress_extend"}, 14)
    r = s.end_turn(3, 14)
    check("budget line: iteration, cap, left, extension state", r["budget_line"],
          "[Budget: iteration 3 of 14, 11 left; extension available (+8 if edits land and verify output changes).]")
    s = B.BudgetState({"budget_visible"}, 14)
    check("budget line without extend policy", s.end_turn(3, 14)["budget_line"].endswith("no extension available.]"), True)

    print("== stale-read stubs ==")
    s = B.BudgetState({"mask_stale_reads"}, 14)
    big = "[read_file a.py: lines 1-100 of 250 (9000 bytes total)]\n" + "x\n" * 100 + "\n[150 more line(s) not shown.]"
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
    for it in range(1, 7):
        s.begin_turn(it)
        res = big if it == 1 else f"other {it}"
        s.observe_tool(it, "read_file", {"path": "a.py" if it == 1 else f"f{it}.py"}, res, readonly=True,
                       msg_index=len(msgs))
        msgs.append({"role": "tool", "content": res})
        s.end_turn(it, 14)
    v = s.mask_view(msgs)
    check("old read stubbed", v[2]["content"], "[read a.py lines 1-100 of 250; omitted -- re-read if you need it]")
    check("recent reads kept", [m["content"] for m in v[-3:]], ["other 4", "other 5", "other 6"])
    check("input list untouched", msgs[2]["content"], big)
    s.edit_seq["a.py"] = 3
    check("edited-since file is NOT masked", s.mask_view(msgs)[2]["content"], big)
    check("no policy -> same object", B.BudgetState(set(), 14).mask_view(msgs) is msgs, True)

    print("== prefetch ==")
    d = Path(tempfile.mkdtemp(prefix="pf-"))
    (d / "m.py").write_text("\n".join(f"line{n}" for n in range(1, 60)) + "\ndef late_fn():\n    pass\nclass K:\n    pass\n")
    out = B.build_prefetch(d, ["m.py"])
    check("head + outline", ("line1" in out and "late_fn" in out and "class K" in out and "line50" not in out), True)
    out = B.build_prefetch(d, ["m.py:3-5"])
    check("named range only", ("line3\nline4\nline5" in out and "line6" not in out and "line2\n" not in out), True)
    check("path escape refused", B.build_prefetch(d, ["../../etc/hosts"]), "")
    check("missing file -> nothing", B.build_prefetch(d, ["nope.py"]), "")
    check("capped", len(B.build_prefetch(d, ["m.py:1-60"], max_chars=100)) < 400, True)
    check("spec parsing from text", B.prefetch_specs_from_text("x\nPrefetch: a.py:1-9, b.py\n"), ["a.py:1-9", "b.py"])

    print("== checkpoint bookkeeping / read streak ==")
    d = Path(tempfile.mkdtemp(prefix="cp-"))
    s = B.BudgetState({"checkpoint_revert"}, 14)
    s.set_cwd(d)
    (d / "b.txt").write_text("v0")
    def edit(it, content):
        s.begin_turn(it)
        s.before_tool("write_file", {"path": "b.txt"}, d)
        (d / "b.txt").write_text(content)
        s.observe_tool(it, "write_file", {"path": "b.txt"}, "OK: wrote b.txt")
    def ver(it, n):
        s.begin_turn(it)
        s.observe_verify(it, f"{n} failed", n == 0)
    edit(1, "good"); ver(2, 2)
    for k, n in enumerate((3, 4, 5)):
        edit(3 + 2 * k, f"bad{n}"); ver(4 + 2 * k, n)
    r = s.end_turn(8, 14, d)
    check("3 worse rounds -> revert fired", r.get("reverted"), ["b.txt"])
    check("best-so-far contents restored", (d / "b.txt").read_text(), "good")
    check("model is told", "Checkpoint revert" in r["notes"][0], True)
    s2 = B.BudgetState({"read_streak_nudge"}, 14)
    nudges = 0
    for it in range(1, 12):
        s2.begin_turn(it)
        s2.observe_tool(it, "read_file", {"path": f"f{it}"}, "x", readonly=True)
        nudges += len(s2.end_turn(it, 30)["notes"])
    check("nudge at 5 and 10 only (max 2)", nudges, 2)
    s2 = B.BudgetState({"read_streak_nudge"}, 14)
    for it in range(1, 5):
        s2.begin_turn(it); s2.observe_tool(it, "read_file", {"path": "f"}, "x", readonly=True); s2.end_turn(it, 30)
    s2.begin_turn(5); s2.observe_tool(5, "write_file", {"path": "f"}, "OK: wrote f"); s2.end_turn(5, 30)
    s2.begin_turn(6); s2.observe_tool(6, "read_file", {"path": "f"}, "x", readonly=True)
    check("a write resets the streak", (s2.end_turn(6, 30)["notes"], s2.read_streak), ([], 1))


# ---------------------------------------------------------------------------------- integration
def tc(name, args):
    return {"message": {"role": "assistant", "content": "",
                        "tool_calls": [{"function": {"name": name, "arguments": args}}]},
            "done_reason": "stop", "usage": {"prompt_tokens": 1000, "completion_tokens": 40}}


def drive(m, script, task="edit a.txt", max_iters=12, verify=None, files=None, **kw):
    wt = Path(tempfile.mkdtemp(prefix="budget-wt-"))
    subprocess.run(["git", "init", "-q", "."], cwd=wt, check=True)
    (wt / "a.txt").write_text("x\n")
    for rel, body in (files or {}).items():
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text(body)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", "i"], cwd=wt, check=True)
    sent, seq = [], list(script)

    def fake_call(host, model, messages, temperature, num_ctx, **k):
        sent.append([dict(x) for x in messages])
        return seq.pop(0) if seq else tc("task_complete", {"summary": "done"})

    m.call_ollama = fake_call
    m.call_ollama_streaming = lambda *a, **k: fake_call(*a, **k)
    m.ensure_model_ready = lambda *a, **k: None
    m.set_keep_alive = lambda *a, **k: None
    m.clamp_unraid_ctx = lambda host, model, n: n
    logs, orig = [], m.log
    m.log = lambda s, *a, **k: logs.append(str(s))
    try:
        m.run_task("stub-model", "http://127.0.0.1:9", str(wt), task, verify, max_iters, 0.0, 65536, None,
                   max_tokens=8192, **kw)
    except SystemExit:
        pass
    except Exception as e:
        import traceback
        logs.append(f"run_task raised: {e!r}\n{traceback.format_exc()}")
    finally:
        m.log = orig
    return sent, "\n".join(logs), wt, dict(m._dispatch_metrics)


def txt(content):
    return {"message": {"role": "assistant", "content": content}, "done_reason": "stop",
            "usage": {"prompt_tokens": 1000, "completion_tokens": 40}}


def w(path, content):
    return tc("write_file", {"path": path, "content": content})


def sh(cmd):
    return tc("run_bash", {"command": cmd})


def user_texts(msgs):
    return [x.get("content") or "" for x in msgs if x.get("role") == "user"]


def integration(m):
    print("== default OFF: no policy is byte-identical to `none`, nothing recorded ==")
    script = [tc("read_file", {"path": "a.txt", "offset": 1, "length": 0}), w("b.txt", "1\n"), w("b.txt", "2\n"),
              w("b.txt", "3\n"), w("b.txt", "4\n")]
    sent0, log0, wt0, met0 = drive(m, script, max_iters=4)
    sent1, log1, wt1, met1 = drive(m, script, max_iters=4, budget_policy="none")
    check("prompts identical with and without `--budget-policy none`",
          json.dumps(sent0).replace(str(wt0), "WT") == json.dumps(sent1).replace(str(wt1), "WT"), True)
    check("no BUDGET log line by default", "BUDGET POLICY" in log0, False)
    check("no budget_* metrics by default", [k for k in met0 if k.startswith("budget_")], [])
    check("still stops at the cap (the baseline we compare against)", "DID NOT CONVERGE after 4 iterations" in log0, True)
    check("no [Budget: line ever sent", any("[Budget:" in t for msgs in sent0 for t in user_texts(msgs)), False)

    print("== progress_extend ==")
    V = "cat vout.txt; grep -q done vout.txt"
    files = {"vout.txt": "x\n"}
    grant = [sh(V), w("vout.txt", "y\n"), sh(V), w("vout.txt", "z\n"), w("vout.txt", "q\n"), w("vout.txt", "r\n"),
             w("vout.txt", "done\n")]
    sent, log, wt, met = drive(m, grant, max_iters=6, verify=V, files=files, budget_policy="progress_extend")
    check("granted at the cap (6 -> ceiling 9: +3) and the run converged", ("progress_extend: +3" in log, "DID NOT CONVERGE" in log), (True, False))
    check("metrics record the grant", (met.get("budget_extension_granted"), met.get("budget_base_cap"),
                                       met.get("budget_policy")), (3, 6, ["progress_extend"]))
    check("transcript carries the grant notice", any("Budget extension granted: +3" in t for msgs in sent for t in user_texts(msgs)), True)
    check("same script WITHOUT the policy dies at the cap", "DID NOT CONVERGE after 6" in drive(
        m, grant, max_iters=6, verify=V, files=files)[1], True)
    files3 = {"vout.txt": "same\n", "b.txt": "0\n"}
    stall = [sh(V), w("b.txt", "1\n"), sh(V), w("b.txt", "2\n"), sh(V), w("b.txt", "3\n")]
    sent, log, wt, met = drive(m, stall, max_iters=6, verify=V, files=files3, budget_policy="progress_extend")
    check("3 identical verify outputs -> refused, run ends at the cap", ("+8" in log, "DID NOT CONVERGE after 6" in log), (False, True))
    check("refusal recorded", "identical" in str(met.get("budget_extension_refused")), True)
    sent, log, wt, met = drive(m, [sh(V), sh(V), sh(V + " ")], max_iters=3, verify=V, files={"vout.txt": "x\n"}, budget_policy="progress_extend")
    check("no write in the last 3 iterations -> refused", "no write" in str(met.get("budget_extension_refused")), True)
    sent, log, wt, met = drive(m, [sh(V), w("vout.txt", "y\n"), sh(V), w("vout.txt", "z\n")] + [w("vout.txt", f"{n}\n") for n in range(30)],
                               max_iters=4, verify=V, files=files, budget_policy="progress_extend")
    check("grant is single-use and clamped to 1.5x (4 -> 6)", ("DID NOT CONVERGE after 6 iterations" in log, met.get("budget_extension_granted")), (True, 2))

    # an iteration that ends via the silent-stop `continue` skips the end-of-iteration block: the cap
    # decision must also be taken by the loop-condition fallback
    silent = [sh(V), w("vout.txt", "y\n"), w("vout.txt", "z\n"), txt("I believe it is done."), w("vout.txt", "done\n")]
    sent, log, wt, met = drive(m, silent, max_iters=4, verify=V, files=files, budget_policy="progress_extend")
    check("cap reached on a silent-stop turn still gets the extension (loop-condition fallback)",
          ("progress_extend: +2" in log, "DID NOT CONVERGE" in log, met.get("budget_extension_granted")), (True, False, 2))

    print("== budget_visible ==")
    sent, log, _, met = drive(m, [tc("read_file", {"path": "a.txt", "offset": 1, "length": 0}), w("b.txt", "1\n"),
                                  tc("list_files", {"path": "."})], max_iters=8, budget_policy="budget_visible")
    lines = [t for t in user_texts(sent[-1]) if t.startswith("[Budget:")]
    check("one notice per completed turn", len(lines), 3)
    check("format", lines[0], "[Budget: iteration 1 of 8, 7 left; no extension available.]")

    print("== read_window ==")
    big = "\n".join(f"row {n}" for n in range(1, 301)) + "\n"
    rf = [tc("read_file", {"path": "big.txt", "offset": 1, "length": 0}), tc("read_file", {"path": "big.txt", "offset": 1, "length": 30})]
    sent, log, _, _ = drive(m, rf, max_iters=3, files={"big.txt": big}, budget_policy="read_window")
    reads = [x["content"] for x in sent[-1] if x.get("role") == "tool" and "big.txt" in str(x.get("content"))[:80]]
    check("default page is 100 lines", "lines 1-100 of 300" in reads[0], True)
    check("a requested 30 is widened to 100", "lines 1-100 of 300" in reads[1], True)
    sent, log, _, _ = drive(m, rf[:1], max_iters=3, files={"big.txt": big})
    reads = [x["content"] for x in sent[-1] if x.get("role") == "tool"]
    check("without the policy the whole file comes back (historical)", reads[0].startswith("row 1\n") or "lines 1-300" in reads[0], True)
    check("window global is reset to 0 when the policy is off", m._READ_WINDOW[0], 0)

    print("== mask_stale_reads ==")
    seq = [tc("read_file", {"path": "big.txt", "offset": 1, "length": 0})] + [tc("list_files", {"path": "d%d" % k}) for k in range(5)]
    sent, log, _, met = drive(m, seq, max_iters=7, files={"big.txt": "\n".join(f"row {n}" for n in range(1, 60)) + "\n"},
                              budget_policy="mask_stale_reads")
    first = [x["content"] for x in sent[-1] if x.get("role") == "tool"][0]
    check("old unedited read is a stub in the prompt", first.startswith("[read big.txt") and "omitted" in first, True)
    first_early = [x["content"] for x in sent[2] if x.get("role") == "tool"][0]
    check("but still full while within the last 3 tool turns", first_early.startswith("row 1"), True)
    seq = [tc("read_file", {"path": "big.txt", "offset": 1, "length": 0}), w("big.txt", "changed\n")] + [tc("list_files", {"path": "d%d" % k}) for k in range(5)]
    sent, log, _, met = drive(m, seq, max_iters=8, files={"big.txt": "\n".join(f"row {n}" for n in range(1, 60)) + "\n"},
                              budget_policy="mask_stale_reads")
    first = [x["content"] for x in sent[-1] if x.get("role") == "tool"][0]
    check("a read of a file edited since is left alone", first.startswith("row 1"), True)

    print("== prefetch_excerpt ==")
    src = "\n".join(f"src line {n}" for n in range(1, 80)) + "\ndef target_fn():\n    pass\n"
    sent, log, _, _ = drive(m, [], task="do it\nPrefetch: mod.py:10-12", max_iters=2, files={"mod.py": src}, budget_policy="prefetch_excerpt")
    first = sent[0][1]["content"]
    check("named range injected into the first prompt", ("PREFETCHED EXCERPT" in first and "src line 10\nsrc line 11\nsrc line 12" in first and "src line 13" not in first), True)
    sent, log, _, _ = drive(m, [], task="do it", max_iters=2, files={"mod.py": src, "AUTO-TASK.md": "Prefetch: mod.py\n"}, budget_policy="prefetch_excerpt")
    check("AUTO-TASK.md marker -> head + outline", ("target_fn" in sent[0][1]["content"] and "src line 1\n" in sent[0][1]["content"]), True)
    sent, log, _, _ = drive(m, [], task="do it", max_iters=2, files={"mod.py": src}, budget_policy="prefetch_excerpt", prefetch_specs=["mod.py:2-3"])
    check("CLI spec", "src line 2\nsrc line 3" in sent[0][1]["content"], True)
    sent, log, _, _ = drive(m, [], task="do it\nPrefetch: mod.py:10-12", max_iters=2, files={"mod.py": src})
    check("policy off -> prompt untouched", sent[0][1]["content"], "do it\nPrefetch: mod.py:10-12")

    print("== checkpoint_revert ==")
    chk = "import sys;t=open('b.txt').read();n=t.count('bad');print(n,'failed');sys.exit(1 if n else 0)"
    V2 = "python3 -c \"%s\"" % chk
    seq = [w("b.txt", "bad bad\n"), sh(V2)]
    for n in (3, 4, 5):
        seq += [w("b.txt", "bad " * n + "\n"), sh(V2)]
    seq += [tc("list_files", {"path": "."})]
    sent, log, wt, met = drive(m, seq, max_iters=len(seq) + 1, verify=V2, files={"b.txt": "bad bad\n"}, budget_policy="checkpoint_revert")
    check("file restored to the best verify state", (wt / "b.txt").read_text(), "bad bad\n")
    check("model told + metrics", (any("Checkpoint revert" in t for msgs in sent for t in user_texts(msgs)), met.get("budget_reverts")), (True, 1))

    print("== read_streak_nudge ==")
    seq = [tc("read_file", {"path": f"f{k}.txt", "offset": 1, "length": 0}) for k in range(6)]
    files = {f"f{k}.txt": f"c{k}\n" for k in range(6)}
    sent, log, _, met = drive(m, seq, max_iters=8, files=files, budget_policy="read_streak_nudge")
    hits = [t for msgs in sent[-1:] for t in user_texts(msgs) if "Write a first draft now" in t]
    check("nudge delivered after 5 read-only turns", len(hits), 1)
    check("not delivered before 5", any("first draft" in t for t in user_texts(sent[4])), False)

    print("== passthrough (task marker / env / research / unknown) ==")
    sent, log, _, met = drive(m, [], task="x\nBudget-policy: budget_visible", max_iters=2)
    check("task marker activates", met.get("budget_policy"), ["budget_visible"])
    os.environ["WORKER_BUDGET_POLICY"] = "read_window"
    try:
        sent, log, _, met = drive(m, [], max_iters=2)
        check("env activates", met.get("budget_policy"), ["read_window"])
        sent, log, _, met = drive(m, [], max_iters=2, budget_policy="none")
        check("explicit none beats env", met.get("budget_policy"), None)
    finally:
        os.environ.pop("WORKER_BUDGET_POLICY", None)
    sent, log, _, met = drive(m, [], max_iters=2, budget_policy="bogus,budget_visible")
    check("unknown ignored with a log line, known kept", ("ignoring unknown" in log, met.get("budget_policy")), (True, ["budget_visible"]))
    sent, log, _, met = drive(m, [], max_iters=2, budget_policy="all", task_kind="research")
    check("research runs ignore policies", "policies apply to coding runs only" in log and not met.get("budget_policy"), True)
    r = subprocess.run([sys.executable, str(WORKER), "--cwd", "/tmp", "--task", "x", "--budget-policy", "nonsense", "--direct-ok"],
                       capture_output=True, text=True)
    check("CLI rejects an unknown name", (r.returncode != 0, "unknown name" in r.stderr), (True, True))


def main():
    unit()
    m = _load_worker()
    integration(m)
    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
