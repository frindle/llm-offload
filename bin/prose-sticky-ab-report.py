#!/usr/bin/env python3
"""prose-sticky-ab-report.py -- read the finished jobs of the PROSE_STICKY_NOTHINK A/B and print the per-arm table + the verdict.

  python3 ~/bin/prose-sticky-ab-report.py                 # table + per-task outcomes + decision-rule verdict
  python3 ~/bin/prose-sticky-ab-report.py --json          # same, machine readable
  python3 ~/bin/prose-sticky-ab-report.py --expected 48   # also say how many of the planned first attempts are still missing
  python3 ~/bin/prose-sticky-ab-report.py --psab2 [--json] [--expected 48]
                                                          # the PROTECT_HARNESS rerun (psab2-<ARM><REP>-<task>, DECISION-RULE-v2.md):
                                                          # same rule, tainted runs (saw refimpl.py content) excluded, Wilson 95% CIs

READ-ONLY: reads ~/bin/ollama-queue-logs/<id>-psab-<ARM><REP>-<task>.log (never pruned) joined to
~/bin/ollama-worker-logs/dispatch-metrics.jsonl by the job's transcript path. Starts nothing, touches no queue job, no GPU.

Arms (set per job by a `Worker-flags:` line in the task text, see ~/.ollama-dispatch/prose-sticky-ab/DECISION-RULE.md):
  A control (no flags)   B PROSE_STICKY_NOTHINK=1   C NOTHINK_RUN=1 (thinking off from iteration 1)

DECISION RULE (fixed BEFORE the run; keep identical to DECISION-RULE.md). Arm X in {B, C} vs control A is a FLIP-CANDIDATE only if
  1 pass_rate(X) >= pass_rate(A)   2 prose-runaway aborts(X) < aborts(A) (strictly)   3 output-cap cut-offs(X) <= those of A
  4 mean wall(X) <= 1.25 x mean wall(A)   5 per-task pass wins >= losses.
A with zero prose-runaway aborts => NO-SIGNAL. <MIN_VALID valid runs in A or X => INCONCLUSIVE. Otherwise NOT-WORTH-IT.
Nothing is flipped by this report; a FLIP-CANDIDATE is a recommendation for the owner."""
import argparse
import glob
import json
import os
import re
import statistics
import subprocess
import sys

HOME = os.path.expanduser("~")
QUEUE_LOGS = os.path.join(HOME, "bin", "ollama-queue-logs")
METRICS = os.path.join(HOME, "bin", "ollama-worker-logs", "dispatch-metrics.jsonl")
BUNDLE = "prose-sticky-ab-20261013"
ARMS = ("A", "B", "C")
ARM_FLAG = {"A": None, "B": "PROSE_STICKY_NOTHINK=1", "C": "NOTHINK_RUN=1"}
ARM_NAME = {"A": "control (sticky OFF)", "B": "PROSE_STICKY_NOTHINK=1", "C": "NOTHINK_RUN=1 (no-think from iter 1)"}
WALL_FACTOR = 1.25      # rule 4: "modest slowdown"
MIN_VALID = 12          # valid runs needed in the control and in an arm before a verdict means anything

_FNAME = re.compile(r"^([0-9a-f]{12})-psab-([ABC])(\d+)-([0-9a-f]{12})\.log$")
# psab2 (PROTECT_HARNESS rerun, bundle prose-sticky-ab2-20261014): <id>-psab2-<ARM><REP>-<task>.log; the $ anchor excludes children
_PSAB2_FNAME = re.compile(r"^([0-9a-f]{12})-psab2-([ABC])(\d+)-([0-9a-f]{12})\.log$")
PSAB2_BUNDLE = "prose-sticky-ab2-20261014"
PSAB2_MANIFEST = os.path.join(HOME, ".ollama-dispatch", "ab-sampling", "v2", "manifest-psab2.json")
PH_ARMED = "PROTECT_HARNESS: sealed coding harness detected"
PH_NOT_ARMED = "PROTECT_HARNESS requested but this is not a sealed coding job"
# refimpl.py template lines: never in target code, so their appearance in a tool result means the reference solution was shown
REFIMPL_SIGNATURES = ("Reference impl for:", "The gate applies this, runs the verify, and reverts it")
TAINT_RUN_LINES, TAINT_RUN_CHARS = 3, 60      # == worker_levers PH_RUN_LINES / PH_RUN_CHARS (the guard's own passage measure)


# ----------------------------------------------------------------------------- parsing (pure)
def parse_run(text, m, arm):
    """One job log (`text`) + its worker metrics row (`m`, dict or None) -> a run dict. Never raises."""
    m = m or {}
    flags = re.findall(r"WORKER FLAGS (.+?) \(from the task", text)
    flag_set = set(" ".join(flags).split())
    want = ARM_FLAG.get(arm)
    ab_flags = {"PROSE_STICKY_NOTHINK=1", "NOTHINK_RUN=1"}
    flag_ok = (want in flag_set) if want else not (flag_set & ab_flags)
    if want and flag_ok and (flag_set & ab_flags) != {want}:
        flag_ok = False
    author = re.findall(r"request profile=\S+ role=author api=\S+ sent=(\{.*\})", text)
    think_first = think_any_off = None

    def _think(s):
        try:
            return (json.loads(s).get("chat_template_kwargs") or {}).get("enable_thinking")
        except Exception:
            return None
    if author:
        think_first = _think(author[0])
        think_any_off = any(_think(s) is False for s in author[1:])
    prose = m.get("prose_runaway_aborts")
    if prose is None:
        prose = len(re.findall(r"ABORTED by the prose-runaway guard", text))
    cuts = m.get("output_cap_prose_cuts")
    if cuts is None:
        cuts = len(re.findall(r"was CUT OFF at the", text))
    term = m.get("terminal_reason") or (re.findall(r"TERMINAL REASON: (\S+)", text) or [None])[-1]
    if term == "prose_loop" and not prose:
        prose = 1
    valid = bool(m) and (m.get("iterations") or 0) > 0 and flag_ok and "model profile:" in text
    why = None if valid else ("no worker metrics row (cancelled / infra failure)" if not m else
                              "arm flag line mismatch" if not flag_ok else "no iterations")
    return {
        "arm": arm, "valid": valid, "invalid_why": why,
        "passed": bool(m.get("verify_passed")) and valid,
        "iters": m.get("iterations"), "max_iters": m.get("max_iters"),
        "prose_aborts": int(prose or 0), "cap_cuts": int(cuts or 0), "terminal": term,
        "l4_engaged": len(re.findall(r"L4: prose runaway seen", text)),
        "wall_s": m.get("wall_time_s"), "prompt_tok": m.get("sum_prompt_tokens"),
        "completion_tok": m.get("sum_completion_tokens"), "tok_s": m.get("tok_s_overall"), "decode_tok_s": m.get("decode_tok_s"),
        "first_author_thinking": think_first, "later_author_thinking_off": think_any_off,
    }


def _mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(statistics.mean(xs), 1) if xs else None


def summarize(runs):
    """runs: {(arm, rep, task): run} -> per-arm aggregates."""
    out = {}
    for arm in ARMS:
        rs = [r for (a, _, _), r in runs.items() if a == arm]
        v = [r for r in rs if r["valid"]]
        p = [r for r in v if r["passed"]]
        out[arm] = {
            "jobs": len(rs), "valid": len(v), "invalid": len(rs) - len(v),
            "pass": len(p), "pass_rate": round(len(p) / len(v), 3) if v else None,
            "iters_all": _mean([r["iters"] for r in v]), "iters_pass": _mean([r["iters"] for r in p]),
            "prose_aborts": sum(r["prose_aborts"] for r in v), "cap_cuts": sum(r["cap_cuts"] for r in v),
            "runs_with_abort": sum(1 for r in v if r["prose_aborts"]), "l4_engaged_runs": sum(1 for r in v if r["l4_engaged"]),
            "wall_mean_s": _mean([r["wall_s"] for r in v]), "prompt_tok_mean": _mean([r["prompt_tok"] for r in v]),
            "completion_tok_mean": _mean([r["completion_tok"] for r in v]),
            "tok_s_mean": _mean([r["tok_s"] for r in v]), "decode_tok_s_mean": _mean([r["decode_tok_s"] for r in v]),
        }
    return out


def per_task(runs):
    """{task: {arm: [pass count, valid count]}}"""
    t = {}
    for (arm, _rep, task), r in runs.items():
        if not r["valid"]:
            continue
        c = t.setdefault(task, {}).setdefault(arm, [0, 0])
        c[1] += 1
        c[0] += 1 if r["passed"] else 0
    return t


def verdict(summ, tasks, min_valid=MIN_VALID, wall_factor=WALL_FACTOR):
    """PURE. -> {arm: {"verdict": FLIP-CANDIDATE|NOT-WORTH-IT|NO-SIGNAL|INCONCLUSIVE, "checks": {...}, "why": str}} for B and C."""
    a = summ["A"]
    res = {}
    for x in ("B", "C"):
        s = summ[x]
        if a["valid"] < min_valid or s["valid"] < min_valid:
            res[x] = {"verdict": "INCONCLUSIVE", "checks": {}, "why": f"valid runs A={a['valid']} {x}={s['valid']} (< {min_valid})"}
            continue
        wins = losses = 0
        for task, d in tasks.items():
            if "A" in d and x in d and d["A"][1] and d[x][1]:
                ra, rx = d["A"][0] / d["A"][1], d[x][0] / d[x][1]
                wins += rx > ra
                losses += rx < ra
        checks = {
            "1 pass_rate >= control": s["pass_rate"] >= a["pass_rate"],
            "2 prose aborts strictly fewer": s["prose_aborts"] < a["prose_aborts"],
            "3 cap cut-offs <= control": s["cap_cuts"] <= a["cap_cuts"],
            "4 mean wall <= %.2fx control" % wall_factor: (s["wall_mean_s"] is not None and a["wall_mean_s"] is not None
                                                          and s["wall_mean_s"] <= wall_factor * a["wall_mean_s"]),
            "5 per-task pass wins >= losses (%d/%d)" % (wins, losses): wins >= losses,
        }
        if a["prose_aborts"] == 0:
            res[x] = {"verdict": "NO-SIGNAL", "checks": checks, "why": "control produced zero prose-runaway aborts; the lever cannot be shown to help"}
        elif all(checks.values()):
            res[x] = {"verdict": "FLIP-CANDIDATE", "checks": checks, "why": "all five rule checks hold (a recommendation for the owner; nothing was flipped)"}
        else:
            res[x] = {"verdict": "NOT-WORTH-IT", "checks": checks, "why": "failed: " + "; ".join(k for k, ok in checks.items() if not ok)}
    return res


# ----------------------------------------------------------------------------- collection (IO)
def load_metrics(path=METRICS):
    met = {}
    try:
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("transcript_path"):
                    met[r["transcript_path"]] = r
    except OSError:
        pass
    return met


def collect(logdir=QUEUE_LOGS, metrics_path=METRICS):
    met = load_metrics(metrics_path)
    runs, dupes = {}, {}
    files = []
    for f in glob.glob(os.path.join(logdir, "*-psab-*.log")):
        mo = _FNAME.match(os.path.basename(f))
        if mo:
            files.append((os.path.getmtime(f), f, mo))
    for _mt, f, mo in sorted(files):                       # a re-run of the same label supersedes the earlier log
        _id, arm, rep, task = mo.groups()
        try:
            text = open(f, errors="ignore").read()
        except OSError:
            continue
        tp = (re.findall(r"CHECKPOINT TRANSCRIPT: (\S+)", text) or [None])[-1]
        key = (arm, int(rep), task)
        if key in runs:
            dupes[key] = dupes.get(key, 1) + 1
        runs[key] = parse_run(text, met.get(tp), arm)
        runs[key]["job"] = _id
    return runs, dupes


def queue_progress(prefix="psab-"):
    """Read-only: (active <prefix> jobs, total <prefix> rows) from `ollama-queue.py status`; (None, None) if unavailable."""
    try:
        out = subprocess.run([sys.executable, os.path.join(HOME, "bin", "ollama-queue.py"), "status"],
                             capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return None, None
    rows = [ln for ln in out.splitlines() if re.match(r"^\[\S+\s*\]\s+[0-9a-f]{12}\s+" + re.escape(prefix), ln)]
    active = [ln for ln in rows if re.match(r"^\[(pending|held|running|queued|scheduled)", ln)]
    return len(active), len(rows)


# ----------------------------------------------------------------------------- output
def render(summ, tasks, ver, runs, expected, dupes, progress):
    L = [f"PROSE_STICKY_NOTHINK A/B  bundle={BUNDLE}  first attempts found={len(runs)}"
         + (f" of {expected} expected" if expected else "")
         + (f"  (queue: {progress[0]} active, {progress[1]} psab rows)" if progress[0] is not None else "")]
    if dupes:
        L.append("  note: re-run labels (latest log used): " + ", ".join(f"{a}{r}-{t}x{n}" for (a, r, t), n in dupes.items()))
    cols = [("arm", "{:<4}"), ("jobs", "{:>4}"), ("valid", "{:>5}"), ("pass", "{:>4}"), ("pass%", "{:>6}"), ("it_all", "{:>7}"),
            ("it_pass", "{:>7}"), ("prose", "{:>5}"), ("cap", "{:>4}"), ("L4run", "{:>5}"), ("wall_s", "{:>8}"), ("prompt_tok", "{:>11}"),
            ("compl_tok", "{:>10}"), ("tok/s", "{:>7}"), ("dec_tok/s", "{:>9}")]
    L.append(" ".join(f"{n:>{len(f.format('')) }}" if n != "arm" else f"{n:<4}" for n, f in cols))
    for arm in ARMS:
        s = summ[arm]
        pr = "-" if s["pass_rate"] is None else f"{100 * s['pass_rate']:.0f}%"
        vals = [arm, s["jobs"], s["valid"], s["pass"], pr, s["iters_all"], s["iters_pass"], s["prose_aborts"], s["cap_cuts"],
                s["l4_engaged_runs"], s["wall_mean_s"], s["prompt_tok_mean"], s["completion_tok_mean"], s["tok_s_mean"], s["decode_tok_s_mean"]]
        L.append(" ".join(f.format("-" if v is None else v) for (_, f), v in zip(cols, vals)))
    L.append("  (prose = prose-runaway aborts summed over valid runs; cap = output-cap cut-offs; L4run = runs where the sticky profile engaged;"
             " wall/tokens are per-run means)")
    L.append("")
    L.append("per-task pass counts (passed/valid), reps pooled:")
    L.append(f"  {'task':<14} " + " ".join(f"{a:>7}" for a in ARMS))
    for task in sorted(tasks):
        L.append(f"  {task:<14} " + " ".join(f"{(str(tasks[task][a][0]) + '/' + str(tasks[task][a][1])) if a in tasks[task] else '-':>7}" for a in ARMS))
    inv = [(k, r["invalid_why"]) for k, r in sorted(runs.items()) if not r["valid"]]
    if inv:
        L.append("invalid runs (excluded): " + "; ".join(f"{k[0]}{k[1]}-{k[2]}: {w}" for k, w in inv))
    wire = [f"{a}: first author turn thinking={sorted({str(r['first_author_thinking']) for (aa, _, _), r in runs.items() if aa == a and r['valid']})}"
            for a in ARMS]
    L.append("wire check (enable_thinking on the first author request): " + "; ".join(wire))
    L.append("")
    L.append("DECISION RULE: X passes if pass_rate>=A, prose aborts strictly < A, cap cuts <= A, wall <= 1.25xA, per-task wins>=losses; A with 0 aborts = NO-SIGNAL")
    for x in ("B", "C"):
        v = ver[x]
        L.append(f"  arm {x} ({ARM_NAME[x]}): {v['verdict']} -- {v['why']}")
        for k, ok in v["checks"].items():
            L.append(f"      [{'x' if ok else ' '}] {k}")
    lost = []
    for task, d in sorted(tasks.items()):
        for x in ("B", "C"):
            if "A" in d and x in d and d[x][1] and d["A"][1] and d[x][0] / d[x][1] < d["A"][0] / d["A"][1]:
                lost.append(f"{x} loses on {task} ({d[x][0]}/{d[x][1]} vs A {d['A'][0]}/{d['A'][1]})")
    L.append("quality tradeoff (tasks where an arm passes fewer reps than control): " + ("; ".join(lost) if lost else "none"))
    return "\n".join(L)


# ----------------------------------------------------------------------------- psab2 (PROTECT_HARNESS rerun)
def wilson(k, n, z=1.96):
    """Wilson score 95% interval for k successes in n trials -> (lo, hi) rounded to 3 places; (None, None) for n == 0."""
    if not n:
        return None, None
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / den
    return round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)


def _norm_lines(text):
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _windows(lines, k=TAINT_RUN_LINES, min_chars=TAINT_RUN_CHARS):
    return {"\n".join(lines[i:i + k]) for i in range(len(lines) - k + 1) if len("".join(lines[i:i + k])) >= min_chars}


def refimpl_windows(refimpl_text, base_texts=()):
    """Passages of refimpl.py that would identify it: k-line windows (>= min chars) NOT present in any other tracked file of the
    sealed base (refimpl's OLD = \"\"\"...\"\"\" anchors quote the original target code; reading the target is not taint)."""
    w = _windows(_norm_lines(refimpl_text))
    for bt in base_texts:
        w -= _windows(_norm_lines(bt))
    return w


def _call_texts(msg):
    """Every string the MODEL produced in one assistant message (content + tool-call arguments, JSON-decoded)."""
    import ast
    out = [msg.get("content") or ""]
    tc = msg.get("tool_calls")
    if isinstance(tc, str):
        try:
            tc = ast.literal_eval(tc)
        except Exception:
            try:
                tc = json.loads(tc)
            except Exception:
                out.append(tc.replace("\\n", "\n"))
                tc = None
    for c in tc or []:
        fn = (c or {}).get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                out.append(args.replace("\\n", "\n"))
                continue
        if isinstance(args, dict):
            out.extend(str(v) for v in args.values())
    return out


def taint_scan(messages, windows):
    """PURE. -> {"tainted": bool, "why": str|None, "refused": int}. A result (tool / user message after the task) is TAINT when it
    carries a refimpl signature line, or a refimpl window the model had not itself written in an EARLIER call (own code echoed
    back is not taint). A refused attempt ("REFUSED: ... refimpl" / "[PROTECT_HARNESS: ... withheld") is counted, never taint."""
    own = []
    refused = 0
    seen_task = False
    for m in messages or []:
        role = m.get("role")
        if role == "assistant":
            own.extend(_call_texts(m))
            continue
        if role not in ("tool", "user"):
            continue
        if role == "user" and not seen_task:
            seen_task = True            # the task text itself (submit-v2 refuses a task that names refimpl)
            continue
        body = m.get("content")
        if not isinstance(body, str):
            body = json.dumps(body, default=str)
        if "[PROTECT_HARNESS:" in body or ("REFUSED:" in body and "refimpl" in body):
            refused += 1
        for sig in REFIMPL_SIGNATURES:
            if sig in body:
                return {"tainted": True, "why": f"tool result shows the refimpl signature line {sig!r}", "refused": refused}
        hit = _windows(_norm_lines(body)) & windows
        if hit:
            own_w = _windows(_norm_lines("\n".join(own)))
            fresh = sorted(hit - own_w)
            if fresh:
                return {"tainted": True, "why": "tool result shows %d refimpl passage(s), e.g. %r" % (len(fresh), fresh[0][:80]),
                        "refused": refused}
    return {"tainted": False, "why": None, "refused": refused}


def load_refimpl_windows(manifest=PSAB2_MANIFEST):
    """{task: windows} from each task's sealed base (git show <base_ref>:refimpl.py, minus passages other tracked files carry)."""
    out = {}
    try:
        tasks = json.load(open(manifest))["tasks"]
    except Exception:
        return out
    for t in tasks:
        repo, ref = t.get("repo"), t.get("base_ref")
        try:
            ref_txt = subprocess.run(["git", "-C", repo, "show", f"{ref}:refimpl.py"], capture_output=True, text=True,
                                     timeout=60).stdout
            pats = [ln for ln in _norm_lines(ref_txt) if len(ln) >= 12][:400]
            files = []
            if pats:
                g = subprocess.run(["git", "-C", repo, "grep", "-F", "-l", *sum((["-e", x] for x in pats), []), ref, "--", ".",
                                    ":(exclude)refimpl.py"], capture_output=True, text=True, timeout=120).stdout.splitlines()
                files = [x.split(":", 1)[1] for x in g if ":" in x]
            base = [subprocess.run(["git", "-C", repo, "show", f"{ref}:{f}"], capture_output=True, text=True, errors="replace",
                                   timeout=60).stdout for f in files[:200]]
            out[t["id"]] = refimpl_windows(ref_txt, base) if ref_txt else set()
        except Exception:
            out[t["id"]] = None              # unknown -> the run is INVALID (taint cannot be ruled out), never silently clean
    return out


def parse_run_psab2(text, m, arm, transcript, windows):
    """parse_run + the v2 validity rules: PROTECT_HARNESS armed in the log, PROTECT_HARNESS=1 among the applied flags,
    a readable transcript, and no taint. Tainted runs are invalid with tainted=True (listed separately)."""
    r = parse_run(text, m, arm)
    r.update(tainted=False, taint_why=None, refused=0)
    flags = set(" ".join(re.findall(r"WORKER FLAGS (.+?) \(from the task", text)).split())
    why = None
    if PH_NOT_ARMED in text or PH_ARMED not in text:
        why = "PROTECT_HARNESS did not arm"
    elif "PROTECT_HARNESS=1" not in flags:
        why = "PROTECT_HARNESS=1 not among the applied Worker-flags"
    elif windows is None:
        why = "refimpl.py of the sealed base unreadable: taint cannot be ruled out"
    elif transcript is None:
        why = "no readable transcript: taint cannot be ruled out"
    if r["valid"] and why:
        r.update(valid=False, passed=False, invalid_why=why)
    if r["valid"]:
        ts = taint_scan(transcript.get("messages") or [], windows)
        r["refused"] = ts["refused"]
        if ts["tainted"]:
            r.update(valid=False, passed=False, tainted=True, taint_why=ts["why"], invalid_why="TAINTED: " + ts["why"])
    return r


def collect_psab2(logdir=QUEUE_LOGS, metrics_path=METRICS, manifest=PSAB2_MANIFEST, windows=None):
    met = load_metrics(metrics_path)
    wins = load_refimpl_windows(manifest) if windows is None else windows
    runs, dupes, files = {}, {}, []
    for f in glob.glob(os.path.join(logdir, "*-psab2-*.log")):
        mo = _PSAB2_FNAME.match(os.path.basename(f))
        if mo:
            files.append((os.path.getmtime(f), f, mo))
    for _mt, f, mo in sorted(files):
        _id, arm, rep, task = mo.groups()
        try:
            text = open(f, errors="ignore").read()
        except OSError:
            continue
        tp = (re.findall(r"CHECKPOINT TRANSCRIPT: (\S+)", text) or [None])[-1]
        tr = None
        try:
            tr = json.load(open(tp)) if tp else None
        except Exception:
            tr = None
        key = (arm, int(rep), task)
        if key in runs:
            dupes[key] = dupes.get(key, 1) + 1
        runs[key] = parse_run_psab2(text, met.get(tp), arm, tr, wins.get(task))
        runs[key]["job"] = _id
    return runs, dupes


def psab2_extras(runs, summ):
    """Wilson 95% intervals per arm (pass rate, runaway-run rate), taint + refused counts."""
    ex = {}
    for arm in ARMS:
        s = summ[arm]
        ex[arm] = {"pass_ci95": wilson(s["pass"], s["valid"]), "runaway_run_rate_ci95": wilson(s["runs_with_abort"], s["valid"]),
                   "tainted": sum(1 for (a, _, _), r in runs.items() if a == arm and r.get("tainted")),
                   "refused_attempts": sum(r.get("refused") or 0 for (a, _, _), r in runs.items() if a == arm)}
    return ex


def verdict_psab2(summ, tasks, runs):
    ver = verdict(summ, tasks)
    tainted = sorted(f"{k[0]}{k[1]}-{k[2]}" for k, r in runs.items() if r.get("tainted"))
    if tainted:
        for x in ver:
            ver[x] = {"verdict": "GUARD-BREACH", "checks": ver[x]["checks"],
                      "why": "tainted run(s) " + ", ".join(tainted) + ": PROTECT_HARNESS has a hole; fix it and re-run those before any verdict"}
    return ver


def render_psab2(summ, tasks, ver, runs, expected, dupes, progress):
    body = render(summ, tasks, ver, runs, expected, dupes, progress).split("\n")
    body[0] = body[0].replace("PROSE_STICKY_NOTHINK A/B  bundle=" + BUNDLE, "PSAB2 (PROTECT_HARNESS=1 every arm)  bundle=" + PSAB2_BUNDLE)
    ex = psab2_extras(runs, summ)
    L = ["", "Wilson 95% intervals (n = valid untainted runs; overlapping intervals => the verdict is directional only):"]
    for arm in ARMS:
        s, e = summ[arm], ex[arm]
        f = lambda ci: "-" if ci[0] is None else f"[{100 * ci[0]:.0f}%, {100 * ci[1]:.0f}%]"   # noqa: E731
        L.append(f"  {arm}: pass {s['pass']}/{s['valid']} {f(e['pass_ci95'])}   runaway runs {s['runs_with_abort']}/{s['valid']} "
                 f"{f(e['runaway_run_rate_ci95'])}   tainted={e['tainted']}   refimpl attempts refused by the guard={e['refused_attempts']}")
    tl = [f"{k[0]}{k[1]}-{k[2]}: {r['taint_why']}" for k, r in sorted(runs.items()) if r.get("tainted")]
    L.append("TAINTED runs (excluded; GUARD-BREACH): " + ("; ".join(tl) if tl else "none"))
    L.append("model-limit labels: none -- an unexplained failure stays 'unexplained' until a falsification test is recorded")
    return "\n".join(body + L)


def main_psab2(a):
    runs, dupes = collect_psab2(a.logdir, a.metrics, a.psab2_manifest)
    summ, tasks = summarize(runs), per_task(runs)
    ver = verdict_psab2(summ, tasks, runs)
    progress = (None, None) if a.no_queue else queue_progress("psab2-")
    if a.json:
        print(json.dumps({"bundle": PSAB2_BUNDLE, "summary": summ, "per_task": tasks, "verdict": ver, "extras": psab2_extras(runs, summ),
                          "runs": {f"{k[0]}{k[1]}-{k[2]}": v for k, v in runs.items()}}, indent=1, default=str))
    else:
        print(render_psab2(summ, tasks, ver, runs, a.expected, dupes, progress))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--expected", type=int, default=0, help="planned first-attempt jobs (48 for the 8x3x2 design)")
    ap.add_argument("--logdir", default=QUEUE_LOGS)
    ap.add_argument("--metrics", default=METRICS)
    ap.add_argument("--no-queue", action="store_true", help="do not call `ollama-queue.py status` for the progress line")
    ap.add_argument("--psab2", action="store_true", help="read the PROTECT_HARNESS rerun (psab2-*, DECISION-RULE-v2.md): taint-excluded, Wilson CIs")
    ap.add_argument("--psab2-manifest", default=PSAB2_MANIFEST, help="with --psab2: the manifest naming each task's repo + sealed base_ref")
    a = ap.parse_args(argv)
    if a.psab2:
        return main_psab2(a)
    runs, dupes = collect(a.logdir, a.metrics)
    summ, tasks = summarize(runs), per_task(runs)
    ver = verdict(summ, tasks)
    progress = (None, None) if a.no_queue else queue_progress()
    if a.json:
        print(json.dumps({"bundle": BUNDLE, "summary": summ, "per_task": tasks, "verdict": ver,
                          "runs": {f"{k[0]}{k[1]}-{k[2]}": v for k, v in runs.items()}}, indent=1, default=str))
    else:
        print(render(summ, tasks, ver, runs, a.expected, dupes, progress))
    return 0


if __name__ == "__main__":
    sys.exit(main())
