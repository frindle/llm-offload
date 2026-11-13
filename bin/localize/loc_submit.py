#!/usr/bin/env python3
"""loc_submit.py -- Mac side of the localizer arms. Submits runner jobs (fallback=False: nothing third-party ever runs
on this Mac), collects the artifacts and normalizes each arm's output.

  loc_submit.py --diag pyrato          one diagnostic job (also installs/warms both tools, dumps --help)
  loc_submit.py --tasks a,b,c          jobs for those tasks (default: every task named in $R/grade*/results.txt)
  loc_submit.py --normalize-only       re-normalize saved raw artifacts (~/offload-research/localize/raw/<task>/)
  loc_submit.py --collect a2a=36cb4de2 fetch a finished job's artifacts after its submitter was killed
  options: --arms sweet,sweetfast,colgrep  --jobs N (<=3)  --query-max CHARS  --force

Output per arm/task: ~/offload-research/localize/out/<arm>/<task>.json
  {"task","arm","tool_version","query_chars","files":[top 20],"functions":[top 50 {"path","symbol","start","end"}],
   "index_s","search_s","raw"}
Timings/log lines: ~/offload-research/localize/runs.jsonl
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
R = Path("/private/tmp/claude-501/-Users-user-Desktop-GitHub-Projects-claude-offload-nosync/"
         "bc93b00c-1a09-40bf-8add-2df5a1fcac65/scratchpad/real")
LOC = Path.home() / "offload-research" / "localize"
VERSIONS = {"sweet": "sweet-search@2.10.5", "sweetfast": "sweet-search@2.10.5 --no-late-interaction", "colgrep": "colgrep v1.8.5 (LateOn-Code-edge default)"}
ARM_NAME = {"sweet": "sweet-search", "sweetfast": "sweet-search-noli", "colgrep": "colgrep"}
SETUP_TIMEOUT = 1800
RUN_TIMEOUT = 6000
LOCK = threading.Lock()


def cpu_job_lib():
    spec = importlib.util.spec_from_file_location("cpu_job", Path.home() / "bin" / "cpu_job.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def all_tasks():
    s = set()
    for rf in sorted(R.glob("grade*/results.txt")):
        for ln in rf.read_text("utf-8", "replace").splitlines():
            f = ln.split()[:3]  # first three fields only
            if len(f) == 3 and f[2] in ("PASS:", "FAIL:"):
                s.add(f[1])
    return sorted(s)


def runner_up():
    p = subprocess.run([sys.executable, str(Path.home() / "bin" / "cpu_lane.py")], capture_output=True, text=True)
    try:
        for r in json.loads(p.stdout)["runners"]:
            if r["runner"] == "unraid-cpu-1":
                return r["seen_s_ago"] < 30
    except Exception:  # noqa: BLE001
        pass
    return False


def log_run(rec):
    with LOCK, open(LOC / "runs.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


def build_query(task, qmax):
    """The issue text (desc.txt) as is, cut to qmax chars. One form for every task and arm."""
    txt = (R / "bench" / task / "desc.txt").read_text("utf-8", "replace").strip()
    return txt[:qmax], len(txt)


def submit(task, mode, arms, qmax, label_extra=""):
    lib = cpu_job_lib()
    repo = R / "bench" / task / "repo"
    st = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True).stdout
    if st.strip():
        raise RuntimeError(f"{task}: repo not clean")
    q, full = build_query(task, qmax)
    tmp = Path(tempfile.mkdtemp(prefix=f"loc-{task}-"))
    (tmp / "query.txt").write_text(q)
    raw = LOC / "raw" / task
    raw.mkdir(parents=True, exist_ok=True)
    dest = raw / "artifacts.tgz"
    logf = open(raw / "job.log", "wb")
    t0 = time.time()
    res = lib.submit_cpu_job(
        str(repo), f'bash "$JOB_TOOLS/loc_job.sh" {"diag" if mode == "diag" else "run"}', RUN_TIMEOUT,
        label=f"loc {mode} {task}{label_extra}", owner=f"localize {task}",
        tools={"loc_job.sh": str(HERE / "loc_job.sh"), "query.txt": str(tmp / "query.txt")},
        env={"DISPATCH_LOC_ARMS": " ".join(arms), "DISPATCH_LOC_TASK": task, "DISPATCH_LOC_TOPK": "50"},
        setup={"cmd": 'bash "$JOB_TOOLS/loc_job.sh" setup', "timeout_s": SETUP_TIMEOUT},
        artifacts=True, artifacts_dest=str(dest), on_log=lambda b: (logf.write(b), logf.flush()),
        fallback=False, claim_timeout_s=14400, api=os.environ.get("CPU_RUNNER_API") or "http://127.0.0.1:7684")
    logf.close()
    rec = {"ts": time.strftime("%FT%T"), "task": task, "mode": mode, "arms": arms, "query_chars": len(q),
           "issue_chars": full, "exit_code": res.exit_code, "timed_out": res.timed_out,
           "infra_error": getattr(res, "infra_error", None), "timings": getattr(res, "timings", None),
           "wall_s": round(time.time() - t0, 1), "job_id": getattr(res, "job_id", None),
           "ran_on": getattr(res, "ran_on", None), "stdout_tail": (res.stdout_tail or "")[-1500:],
           "stderr_tail": (res.stderr_tail or "")[-800:]}
    log_run(rec)
    ap = getattr(res, "artifacts_path", None)
    if ap and Path(ap).is_file():
        merge_artifacts(task, ap)
    return rec


def merge_artifacts(task, tgz):
    """Unpack a job's artifacts into raw/<task>/x. Files from this job replace same-named ones; files of arms this
    job did not run are kept, so a colgrep-only job and a later sweet-only job add up."""
    raw = LOC / "raw" / task
    ex, new = raw / "x", Path(tempfile.mkdtemp(prefix="new-", dir=raw))
    with tarfile.open(tgz) as tf:
        tf.extractall(new, filter="data")  # job output is untrusted
    ex.mkdir(exist_ok=True)
    for f in new.rglob("*"):
        if f.is_file():
            d = ex / f.relative_to(new)
            d.parent.mkdir(parents=True, exist_ok=True)
            os.replace(f, d)
    subprocess.run(["rm", "-rf", str(new)])


def collect(task, job_prefix, qmax):
    """Fetch the artifacts of a finished job whose submitter is gone (blobs stay on the Mac for 7 days)."""
    import sqlite3
    db = sqlite3.connect("file:%s?mode=ro" % (Path.home() / ".ollama-dispatch/cpu-jobs/jobs.sqlite"), uri=True)
    ids = [r[0] for r in db.execute("select id from jobs where id like ?", (job_prefix + "%",))]
    if len(ids) != 1:
        raise SystemExit(f"{task}: job prefix {job_prefix!r} matches {len(ids)} jobs")
    lib, api = cpu_job_lib(), os.environ.get("CPU_RUNNER_API") or "http://127.0.0.1:7684"
    tok = lib._token()
    code, st = lib._http(api, tok, "GET", "/api/cpu/jobs/%s" % ids[0])
    res = (st or {}).get("result") or {}
    if code != 200 or (st or {}).get("status") != "done" or not res.get("artifacts"):
        raise SystemExit(f"{task}: job {ids[0][:12]} not collectable (http {code}, status {(st or {}).get('status')})")
    raw = LOC / "raw" / task
    raw.mkdir(parents=True, exist_ok=True)
    path, err = lib._fetch_artifacts(api, tok, ids[0], res["artifacts"], str(raw / "artifacts.tgz"))
    if err:
        raise SystemExit(f"{task}: {err}")
    merge_artifacts(task, path)
    log_run({"ts": time.strftime("%FT%T"), "task": task, "mode": "collect", "job_id": ids[0],
             "exit_code": res.get("exit_code"), "timings": res.get("timings"),
             "stdout_tail": (res.get("stdout_tail") or "")[-1500:]})
    return normalize(task, len(build_query(task, qmax)[0]))


# ---- normalization (job output is untrusted: only parsed as text/JSON) ----
def rel(p, root_hint=None):
    p = str(p).strip()
    p = re.sub(r"^\./", "", p)
    if p.startswith("/"):
        m = re.search(r"/checkout/(.*)$", p)
        if m:
            return m.group(1)
        return p.lstrip("/")
    return p


def uniq(xs, n):
    seen, out = set(), []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out[:n]


def norm_colgrep(raw_json_text):
    """colgrep --json: [{"unit": {"file","name","line","end_line",...}, "score"}, ...] (per its README)."""
    data = json.loads(raw_json_text)
    if isinstance(data, dict):
        data = data.get("results") or data.get("hits") or []
    files, funcs = [], []
    for h in data:
        u = h.get("unit", h)
        f = rel(u.get("file") or u.get("path") or "")
        if not f:
            continue
        files.append(f)
        funcs.append({"path": f, "symbol": u.get("name") or u.get("qualified_name") or "",
                      "start": u.get("line") or u.get("start_line"), "end": u.get("end_line") or u.get("end")})
    return uniq(files, 20), funcs[:50]


PATH_RE = re.compile(r"(?P<p>(?:[\w.\-@+]+/)*[\w.\-@+]+\.(?:py|js|ts|tsx|jsx|rs|go|java|c|h|cpp|md|rst|txt|toml|ya?ml|json|cfg|ini))"
                     r"(?:[:#](?P<s>\d+)(?:[-:](?P<e>\d+))?)?")


def norm_sweet(text):
    """Best effort on sweet-search's text/JSON output: ranked 'path:start-end' mentions in order of appearance.
    Refined after the diagnostic job showed the real format (see report.md)."""
    files, funcs = [], []
    try:
        data = json.loads(text)
        seen = set()
        for h in data.get("results", []):
            md = h.get("metadata") or {}
            f = rel(h.get("file") or h.get("file_path") or md.get("file") or "")
            if not f:
                continue
            files.append(f)
            sym = h.get("name") or md.get("name") or ""
            st = h.get("startLine") or h.get("start_line") or md.get("startLine")
            en = h.get("endLine") or h.get("end_line") or md.get("endLine")
            if sym and (f, sym) not in seen:
                seen.add((f, sym))
                funcs.append({"path": f, "symbol": sym, "start": st, "end": en})
        return uniq(files, 20), funcs[:50]
    except Exception:  # noqa: BLE001
        files, funcs = [], []
    for m in PATH_RE.finditer(text):
        f = rel(m.group("p"))
        files.append(f)
        if m.group("s"):
            funcs.append({"path": f, "symbol": "", "start": int(m.group("s")),
                          "end": int(m.group("e")) if m.group("e") else None})
    return uniq(files, 20), funcs[:50]


def normalize(task, qchars):
    ex = LOC / "raw" / task / "x"
    outs = {}
    for arm in ("colgrep", "sweet", "sweetfast"):
        tj = ex / f"{arm}.times.json"
        if not tj.is_file():
            continue
        times = json.loads(tj.read_text())
        files, funcs, rawtxt, err = [], [], "", None
        try:
            if arm == "colgrep":
                rawtxt = (ex / "colgrep.search.json").read_text("utf-8", "replace")
                files, funcs = norm_colgrep(rawtxt)
            else:
                rawtxt = (ex / f"{arm}.search.out").read_text("utf-8", "replace")
                files, funcs = norm_sweet(rawtxt)
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        d = {"task": task, "arm": ARM_NAME[arm], "tool_version": VERSIONS[arm], "query_chars": qchars,
             "files": files, "functions": funcs, "index_s": times.get("index_s"), "search_s": times.get("search_s"),
             "raw": rawtxt[:200_000]}
        if err:
            d["error"] = err
        od = LOC / "out" / ARM_NAME[arm]
        od.mkdir(parents=True, exist_ok=True)
        (od / f"{task}.json").write_text(json.dumps(d, indent=1))
        outs[arm] = (len(files), err)
    return outs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--diag")
    ap.add_argument("--tasks", default="")
    ap.add_argument("--arms", default="sweet,colgrep")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--query-max", type=int, default=2000)
    ap.add_argument("--normalize-only", action="store_true")
    ap.add_argument("--force", action="store_true", help="redo tasks that already have output")
    ap.add_argument("--no-runner-check", action="store_true")
    ap.add_argument("--collect", default="", help="TASK=JOBID[,TASK=JOBID]: fetch finished jobs' artifacts")
    a = ap.parse_args()
    arms = [x for x in a.arms.split(",") if x]
    LOC.mkdir(parents=True, exist_ok=True)
    if a.collect:
        for pair in a.collect.split(","):
            t, j = pair.split("=", 1)
            print(t, collect(t, j, a.query_max))
        return
    if a.normalize_only:
        for t in (a.tasks.split(",") if a.tasks else all_tasks()):
            if (LOC / "raw" / t / "x").is_dir():
                print(t, normalize(t, len(build_query(t, a.query_max)[0])))
        return
    if not a.no_runner_check and not runner_up():
        sys.exit("unraid-cpu-1 not seen in the last 30 s: not submitting (will not poll).")
    if a.diag:
        rec = submit(a.diag, "diag", arms, a.query_max)
        print(json.dumps({k: rec[k] for k in ("exit_code", "infra_error", "wall_s", "timings")}, indent=1))
        print(normalize(a.diag, rec["query_chars"]))
        return
    tasks = a.tasks.split(",") if a.tasks else all_tasks()
    if not a.force:
        tasks = [t for t in tasks if not all((LOC / "out" / ARM_NAME[x] / f"{t}.json").is_file() for x in arms)]
    jobs = max(1, min(3, a.jobs))  # runner is shared: never more than 3 at once

    def one(t):
        try:
            rec = submit(t, "run", arms, a.query_max)
            n = normalize(t, rec["query_chars"])
            print(f"{t}: exit={rec['exit_code']} infra={rec['infra_error']} wall={rec['wall_s']}s {n}", flush=True)
        except Exception as e:  # noqa: BLE001
            log_run({"ts": time.strftime("%FT%T"), "task": t, "error": f"{type(e).__name__}: {e}"})
            print(f"{t}: ERROR {type(e).__name__}: {e}", flush=True)

    with ThreadPoolExecutor(jobs) as ex:
        list(ex.map(one, tasks))


if __name__ == "__main__":
    main()
