#!/usr/bin/env python3
"""Offload (mini-swe-runner) job progress on the dashboard (2026-11-03). A running offload job shows "iter N/M" (reply N of
the current agent run's step limit) and, while it waits for Darkbloom (a sandbox hold, a model reload), the phase
'outage': "waiting for Darkbloom (m:ss)" instead of a row that looks frozen. 'preparing' covers the runner's work-dir
setup. Runs the API's own _mini_progress/_job_summary on synthetic live logs and the page's own laneStatusLine()
under node.
Run: python3 tests/test-dashboard-offload-progress.py [--src PATH] [--mutants]
  --mutants applies each revert mutant to the source and expects this test to FAIL on each."""
import importlib.util, json, os, re, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
REAL_HOME = os.environ["HOME"]
FAILS = []

MUTANTS = [
    ("a reply does not end an outage",
     "st.update(mini=True, step=int(m.group(1)), outage=False, outage_s=None)", "st.update(mini=True, step=int(m.group(1)))"),
    ("a late usage line ends the outage",
     "if line.startswith(_MINI_RUNNER) and not _MINI_POLL_STEP_RE.match(line):", "if line.startswith(_MINI_RUNNER):"),
    ("retried calls are not an outage",
     "    if _MINI_RETRY_RE.match(line):\n        st[\"outage\"] = True\n        return\n", ""),
    ("the step limit is only looked for on the specs' first line",
     "_MINI_SPEC_LINES = 16 ", "_MINI_SPEC_LINES = 1 "),
    ("the specs window does not close at the list's end",
     "        if m or line.rstrip().endswith(b\"]\"):\n", "        if m:\n"),
    ("half-written lines are parsed",
     "end = buf.rfind(b\"\\n\") + 1 ", "end = len(buf) "),
    ("a truncated log is not rescanned",
     " or fs.st_size < st[\"off\"]:", ":"),
    ("the row ignores offload progress",
     "    if mini:\n        iteration, max_iters = mini[\"step\"], mini[\"limit\"]",
     "    if False:\n        iteration, max_iters = mini[\"step\"], mini[\"limit\"]"),
    ("the lane line hides the outage",
     "  if (j.phase === 'outage') bits.push(outageText(j));\n", ""),
    ("the status chip ignores the new phases",
     "const PHASE_LABEL = {warming: 'warming', outage: 'waiting for Darkbloom', preparing: 'preparing'};",
     "const PHASE_LABEL = {warming: 'warming'};"),
    ("a run without a step limit shows no progress",
     ": j.max_iters != null ? j.iteration + '/' + j.max_iters : String(j.iteration);",
     ": j.max_iters != null ? j.iteration + '/' + j.max_iters : '';"),
]

SPECS = ("Building agent config from specs: ['mini.yaml', \n'/Users/me/bin/configs/mini-local.yaml', \n"
         "'environment.cwd=/Users/me/.ollama-dispatch/mini-work/x-202611\n02235855/work', 'agent.step_limit={n}', "
         "'agent.wall_time_limit_seconds=1320']\n\nSystem:\nYou are a helpful assistant that can interact with a computer.\n")
RETRY = ("Retrying <unknown> in 4 seconds as it raised BadRequestError: litellm.BadRequestError: OpenAIException - "
         "Provider capacity is temporarily unavailable..\n")


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load(src):
    home = tempfile.mkdtemp(prefix="offload-progress-")
    os.environ["HOME"] = home
    os.symlink(os.environ.get("OLLAMA_PIPELINE_BIN", str(Path(REAL_HOME) / "bin")), os.path.join(home, "bin"))
    ld = SourceFileLoader("qapi_offload", str(src))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("qapi_offload", ld))
    ld.exec_module(m)
    return m


def node(js):
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    return json.loads(out.stdout) if out.returncode == 0 else {"err": out.stderr[-400:]}


def api_checks(m):
    d = Path(tempfile.mkdtemp(prefix="offload-logs-"))
    log = d / "j.livelog"

    def app(s):
        with open(log, "ab") as f:
            f.write(s.encode())

    def prog():
        return m._mini_progress(str(log))

    app("[mini-runner] === step 1/3: ingest/t.py ===\n[mini-runner] preparing the work dir from /w\n")
    check("setup: preparing", (prog() or {}).get("phase"), "preparing")
    app("[mini-runner] still preparing the work dir from /w (30s)\n")
    check("setup heartbeat: still preparing", (prog() or {}).get("phase"), "preparing")
    app("[mini-runner] work dir ready (31s)\n[mini-runner] phase B attempt 1 (2158s)\n" + SPECS.format(n=66))
    check("run start: step 0 of the wrapped specs' limit", prog(), {"step": 0, "limit": 66, "phase": None, "outage_s": None})
    app("mini-swe-agent (step 1, $0.00):\nTHOUGHT: grep for 'agent.step_limit=5' in the runner\n```\ngrep -n x y\n```\n"
        "'agent.step_limit=5'\n")
    check("a quoted limit far outside the specs block is not the limit", (prog() or {}).get("limit"), 66)
    app("mini-swe-agent (step 2, $0.00):\nok\n" + RETRY + RETRY)
    check("retried call: outage, length not yet known", prog(), {"step": 2, "limit": 66, "phase": "outage", "outage_s": None})
    app("[mini-runner] step 2: 13139 in / 2584 out (69.8 tok/s wall incl. prefill+tools, 37s since prev reply)\n")
    check("a late usage line keeps the outage", (prog() or {}).get("phase"), "outage")
    app("[provider-outage] unavailable (BadRequestError); waiting 15s, outage so far 280s of 604800s "
        "(waiting for Darkbloom, phase=idle, 5m)\n" + RETRY)
    check("mini_ctx's report: outage length", ((prog() or {}).get("phase"), (prog() or {}).get("outage_s")), ("outage", 280))
    app("mini-swe-agent (step 3")
    check("a half-written reply line is not read yet", ((prog() or {}).get("step"), (prog() or {}).get("phase")), (2, "outage"))
    app(", $0.00):\n")
    check("the next reply ends the outage", prog(), {"step": 3, "limit": 66, "phase": None, "outage_s": None})
    app(RETRY)
    check("a new failed call: outage again", (prog() or {}).get("phase"), "outage")
    app("[mini-runner] fix: provider unavailable 600s (excluded from the wall/total budgets)\n")
    check("a runner line ends the outage", prog(), {"step": 3, "limit": 66, "phase": None, "outage_s": None})
    app("[mini-runner] phase B attempt 2 (1500s)\nBuilding agent config from specs: ['mini.yaml', \n'/c.yaml', \n'environment.cwd=/w']\n\n")
    check("a run started without a limit: step 0, limit unknown", prog(), {"step": 0, "limit": None, "phase": None, "outage_s": None})
    app("mini-swe-agent (step 3, $0.00):\n")
    check("the new run counts its own steps", ((prog() or {}).get("step"), (prog() or {}).get("limit")), (3, None))

    # the same log read in tiny chunks (many polls) ends in the same state as one read
    whole = m._mini_progress(str(log))
    m._MINI_SCAN.clear()
    chunk = m._MINI_SCAN_CHUNK
    m._MINI_SCAN_CHUNK = 37
    try:
        polls, got = 0, None
        while polls < 10000:
            polls += 1
            got = m._mini_progress(str(log))
            if m._MINI_SCAN[str(log)]["off"] == log.stat().st_size:
                break
        check("chunked reads reach the same state", got, whole)
        check("chunked reads take many polls (bounded read per poll)", polls > 10, True)
    finally:
        m._MINI_SCAN_CHUNK = chunk

    other = d / "w.log"
    other.write_text("--- iteration 3/24 ---\n[worker] step (41.2 tok/s)\n")
    check("a worker log is not an offload log", m._mini_progress(str(other)), None)
    check("a missing log", m._mini_progress(str(d / "nope.log")), None)
    check("no log path", m._mini_progress(None), None)

    m._MINI_SCAN.clear()
    for i in range(m._MINI_SCAN_MAX + 5):
        p = d / f"n{i}.livelog"
        p.write_text("[mini-runner] work dir ready (0s)\n")
        m._mini_progress(str(p))
    check("tracked logs are bounded", len(m._MINI_SCAN), m._MINI_SCAN_MAX)

    m._mini_progress(str(log))                  # tracked again, read to its end
    log.write_text("[mini-runner] preparing the work dir from /again\n")
    check("a truncated log is rescanned", (m._mini_progress(str(log)) or {}).get("phase"), "preparing")

    row = m._job_summary({"id": "r", "label": "l", "status": "running", "log_path": str(log)})
    check("row: phase rides on the running job", (row["phase"], row["outage_s"]), ("preparing", None))
    app("[mini-runner] work dir ready (0s)\n" + SPECS.format(n=40) + "mini-swe-agent (step 7, $0.00):\n" + RETRY
        + "[provider-outage] unavailable (BadRequestError); waiting 30s, outage so far 575s of 604800s "
        "(waiting for Darkbloom, phase=idle, 10m)\n")
    row = m._job_summary({"id": "r", "label": "l", "status": "running", "log_path": str(log)})
    check("row: iter, limit, outage phase and length",
          (row["iteration"], row["max_iters"], row["phase"], row["outage_s"]), (7, 40, "outage", 575))
    done = m._job_summary({"id": "r", "label": "l", "status": "failed", "log_path": str(log)})
    check("row: a finished job carries no live phase", (done["phase"], done["outage_s"]), (None, None))


def js_checks(m):
    html = m.FRONTEND_HTML
    lanes = re.search(r"const NOW_LANES = .*?function laneNowHtml\(rows\) \{.*?\n\}\n", html, re.S)
    fe = re.search(r"function formatElapsed\(s\) \{.*?\n\}\n", html, re.S)
    check("lane helpers and formatElapsed present", bool(lanes and fe), True)
    if not (lanes and fe):
        return
    pre = "const escapeHtml=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');\n" + fe.group(0) + lanes.group(0) + "\n"
    jobs = {
        "outage": {"status": "running", "phase": "outage", "outage_s": 575, "iteration": 9, "max_iters": 66, "tok_s": 39.3},
        "outage_early": {"status": "running", "phase": "outage", "outage_s": None, "iteration": 9, "max_iters": 66},
        "preparing": {"status": "running", "phase": "preparing"},
        "no_limit": {"status": "running", "iteration": 5, "max_iters": None, "tok_s": 10},
        "worker": {"status": "running", "iteration": 3, "max_iters": 24, "tok_s": 41.2},
        "warming": {"status": "running", "phase": "warming"},
        "fresh": {"status": "running"},
    }
    r = node(pre + "const J=" + json.dumps(jobs) + ";const out={};for (const k in J) out[k]=[laneStatusLine(J[k]), "
             "PHASE_LABEL[J[k].phase] || J[k].status];console.log(JSON.stringify(out));")
    check("node ran the page's code", "err" in r, False)
    got = {k: v[0] for k, v in r.items() if k != "err"}
    check("lane line: outage with its length, no stale tok/s", got.get("outage"), "waiting for Darkbloom (9:35) - iter 9/66")
    check("lane line: outage before mini_ctx's first report", got.get("outage_early"), "waiting for Darkbloom - iter 9/66")
    check("lane line: work-dir setup", got.get("preparing"), "preparing the work dir")
    check("lane line: a run without a limit still shows its step", got.get("no_limit"), "iter 5 - 10.0 tok/s")
    check("lane line: worker jobs unchanged", got.get("worker"), "iter 3/24 - 41.2 tok/s")
    check("lane line: warming unchanged", got.get("warming"), "loading model")
    check("lane line: nothing known yet", got.get("fresh"), "running")
    chips = {k: v[1] for k, v in r.items() if k != "err"}
    check("status chip labels", (chips.get("outage"), chips.get("preparing"), chips.get("warming"), chips.get("worker")),
          ("waiting for Darkbloom", "preparing", "warming", "running"))
    check("row chip uses the phase label", "let statusLabel = PHASE_LABEL[j.phase] || j.status;" in html, True)
    check("row chip class follows the phase", "const stCls = PHASE_LABEL[j.phase] ? j.phase : j.status;" in html, True)
    check("outage and preparing chips are styled", ".chip.outage {" in html and ".chip.preparing {" in html, True)
    check("running-now tile shows the outage", "j.phase === 'outage' ? `<span><b>${escapeHtml(outageText(j))}</b></span>`" in html, True)


def main():
    src = Path(sys.argv[sys.argv.index("--src") + 1]) if "--src" in sys.argv else HERE.parent / "src" / "ollama-queue-api.py"
    if "--mutants" in sys.argv:
        text = src.read_text()
        bad = 0
        for name, old, new in MUTANTS:
            if text.count(old) != 1:
                print(f"FAIL: mutant anchor missing or ambiguous: {name}"); bad += 1; continue
            tmp = src.parent / ".mutant-ollama-queue-api.py"     # next to its sibling modules
            tmp.write_text(text.replace(old, new, 1))
            try:
                r = subprocess.run([sys.executable, __file__, "--src", str(tmp)], capture_output=True, text=True)
            finally:
                tmp.unlink(missing_ok=True)
            red = r.returncode != 0 and "FAIL:" in r.stdout and "crashed" not in r.stdout
            print(("ok  " if red else "FAIL") + f": mutant goes red -- {name}")
            bad += 0 if red else 1
        return 1 if bad else 0
    try:
        m = load(src)
        api_checks(m)
        js_checks(m)
    except Exception as e:
        import traceback; traceback.print_exc(); FAILS.append(f"crashed: {e}")
    print(f"\n{len(FAILS)} failure(s)" if FAILS else "\nALL PASSED")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
