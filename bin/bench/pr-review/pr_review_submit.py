#!/usr/bin/env python3
"""pr_review_submit.py -- queue the PR-review backtest: one code-review-agent.py job per (item, model, repeat).

  submit : pr_review_submit.py submit --run NAME --models m1,m2 [--items FILE|N] [--repeats 3] [--refute-from RUN] [--dry-run]
  fetch  : pr_review_submit.py fetch  --run NAME            (downloads finished jobs' result dirs; safe to repeat)
  status : pr_review_submit.py status --run NAME

Every model call goes through the ollama queue (upload package -> the queue's own code-review-agent.py runner). Nothing
here talks to Ollama. The runner only ever sees diff.patch plus the PR title; keys.json is never read by this tool.
Jobs are ordered model-major so the GPU swaps models as little as possible. Labels start with `bakeoff-prr-` (the queue
honours a host pin for bakeoff labels and treats them as background work).
"""
import argparse, json, os, re, sys, tarfile, tempfile, time
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/bin"))
CORPUS = Path(os.environ.get("PRR_CORPUS", "~/bench/pr-review-corpus")).expanduser()
RUNNER = "code-review-agent.py"


def slug(m):
    return re.sub(r"[^a-z0-9]+", "", m.lower())[:14]


def ctx_for(model, override=None):
    if override and model in override:
        return int(override[model])
    m = re.search(r"(\d+)b", model.lower())
    return 16384 if (m and int(m.group(1)) >= 12) else 32768


def plan(items, models, repeats, ctx_override=None, host="unraid"):
    """Pure: the ordered job list. Model-major, then repeat, then item."""
    jobs = []
    for model in models:
        for rep in range(1, repeats + 1):
            for iid in items:
                jobs.append({"item": iid, "model": model, "repeat": rep, "host": host,
                             "num_ctx": ctx_for(model, ctx_override),
                             "label": f"bakeoff-prr-{slug(model)}-{iid}-r{rep}"[:60]})
    return jobs


def build(job, item_meta, corpus, dest):
    import queue_remote as QR
    with tempfile.TemporaryDirectory(prefix="prr-") as td:
        spec = Path(td) / "spec.json"
        spec.write_text(json.dumps({"mode": "review", "diff": "diff.patch", "intent": item_meta["intent"]}))
        tree = Path(td) / "tree"
        tree.mkdir()
        (tree / "diff.patch").write_text((corpus / "items" / job["item"] / "diff.patch").read_text())
        stage = Path(td) / "stage"
        stage.mkdir()
        ent = QR.stage_tree("d", str(tree), str(stage))
        man = {"version": 1, "runner": RUNNER, "trees": [ent],
               "refs": [{"path": ["diff"], "tree": "d", "rel": "diff.patch"}],
               "job": {"model": job["model"], "label": job["label"], "host": job["host"], "task_kind": "coding",
                       "num_ctx": job["num_ctx"], "allow_duplicate_label": True}}
        (stage / "manifest.json").write_text(json.dumps(man))
        (stage / "spec.json").write_text(spec.read_text())
        with tarfile.open(dest, "w") as tf:
            for n in ("manifest.json", "spec.json", "trees"):
                tf.add(stage / n, arcname=n)


def runs_dir(run):
    d = CORPUS / "runs" / run
    d.mkdir(parents=True, exist_ok=True)
    return d


def cmd_submit(a):
    import queue_remote as QR, queue_config
    items_meta = json.loads((CORPUS / "items.json").read_text())
    ids = sorted(items_meta)
    if a.items:
        if a.items.isdigit():
            rng = __import__("random").Random(7)
            ids = sorted(rng.sample(ids, int(a.items)))
        else:
            ids = [l.strip() for l in Path(a.items).read_text().split() if l.strip() in items_meta]
    jobs = plan(ids, a.models.split(","), a.repeats, json.loads(a.ctx) if a.ctx else None, a.host)
    rd = runs_dir(a.run)
    jf = rd / "jobs.json"
    done = json.loads(jf.read_text()) if jf.exists() else []
    have = {j["label"] for j in done}
    todo = [j for j in jobs if j["label"] not in have]
    print(f"{len(jobs)} planned, {len(todo)} to submit")
    if a.dry_run:
        return 0
    base, _ = queue_config.queue_api()
    headers = queue_config.queue_api_headers()
    for j in todo:
        with tempfile.TemporaryDirectory() as td:
            pkg = Path(td) / "p.tar"
            build(j, items_meta[j["item"]], CORPUS, pkg)
            j["job_id"] = str(QR.upload(base, headers, pkg).get("id") or "")
        j["submitted"] = int(time.time())
        done.append(j)
        jf.write_text(json.dumps(done, indent=1))
    print("submitted", len(todo), "jobs; first", todo[0]["job_id"] if todo else "-", "last", todo[-1]["job_id"] if todo else "-")
    return 0


def queue_rows():
    import urllib.request, queue_config
    base, _ = queue_config.queue_api()
    r = urllib.request.urlopen(urllib.request.Request(base + "/api/state", headers=queue_config.queue_api_headers()), timeout=60)
    st = json.loads(r.read())
    return {str(j.get("id")): j for j in (st.get("jobs") or [])} if isinstance(st.get("jobs"), list) else {}


def cmd_status(a):
    jobs = json.loads((runs_dir(a.run) / "jobs.json").read_text())
    rows = queue_rows()
    from collections import Counter
    c = Counter((rows.get(j["job_id"]) or {}).get("status", "gone/done") for j in jobs)
    print(dict(c))
    return 0


def cmd_fetch(a):
    import queue_remote as QR, queue_config
    rd = runs_dir(a.run)
    jobs = json.loads((rd / "jobs.json").read_text())
    base, _ = queue_config.queue_api()
    headers = queue_config.queue_api_headers()
    got = miss = 0
    for j in jobs:
        dest = rd / "results" / j["job_id"]
        if (dest / "run.json").exists() or (dest / "report.md").exists() or (dest / "skipped.json").exists():
            got += 1
            continue
        try:
            QR.fetch_result(base, headers, j["job_id"], dest)
            fin = any((dest / n).exists() for n in ("run.json", "report.md", "skipped.json"))
            try:
                st = json.loads((dest / "job.json").read_text()).get("status")
            except (OSError, ValueError):
                st = None
            if fin or st in ("failed", "error", "cancelled"):
                got += 1
            else:
                miss += 1
        except Exception:
            miss += 1
    print(f"fetched/have {got}, not ready {miss}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("submit")
    s.add_argument("--run", required=True)
    s.add_argument("--models", required=True)
    s.add_argument("--items")
    s.add_argument("--repeats", type=int, default=3)
    s.add_argument("--ctx", help='JSON {"model": num_ctx}')
    s.add_argument("--host", default="unraid")
    s.add_argument("--dry-run", action="store_true")
    for n in ("fetch", "status"):
        sp.add_parser(n).add_argument("--run", required=True)
    a = ap.parse_args(argv)
    return {"submit": cmd_submit, "fetch": cmd_fetch, "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
