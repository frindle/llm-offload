#!/usr/bin/env python3
"""queue-backtest on made-up fixtures: every part passes on matching data and catches a planted difference.
Nothing here is real history. Runs the real code of this tree (new bin) and a tiny fake old bin."""
import http.server, json, os, shutil, subprocess, sys, tempfile, threading

HERE = os.path.dirname(os.path.realpath(__file__))
bad = 0
nrun = 0


def check(label, ok):
    global bad
    print(("ok   " if ok else "FAIL ") + label)
    bad += 0 if ok else 1


def w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write(text)


def run(t, parts, extra=(), env_extra=None):
    global nrun
    nrun += 1
    out = os.path.join(t, "state", ".backtest", "r%d" % nrun)
    env = dict(os.environ, QUEUE_STATE_DIR=os.path.join(t, "state"), QUEUE_DISPATCH_DIR=os.path.join(t, "disp"),
               HOME=os.path.join(t, "home"), QUEUE_BACKTEST_DIR=os.path.join(t, "state", ".backtest"))
    env.pop("QUEUE_STANDBY", None)
    env.update(env_extra or {})
    p = subprocess.run([sys.executable, os.path.join(HERE, "queue-backtest"), "run", "--out", out, "--parts", parts,
                        "--cases", os.path.join(t, "cases")] + list(extra), capture_output=True, text=True, env=env)
    try:
        res = json.load(open(os.path.join(out, "results.json")))
    except Exception:
        res = None
        print(p.stdout[-400:], p.stderr[-600:])
    return p.returncode, res, out


with tempfile.TemporaryDirectory() as t:
    os.makedirs(os.path.join(t, "home"))
    st, disp, cases = (os.path.join(t, x) for x in ("state", "disp", "cases"))
    # fake old bin: differs from the real code on purpose
    old = os.path.join(t, "oldbin")
    w(os.path.join(old, "ollama-queue.py"),
      "def default_bundle_from_label(label):\n    return str(label)\n"
      "def resolve_ctx_ceiling(h, m):\n    return 99999\n"
      "def darkbloom_view(now=None, log_path=None, slots_path=None):\n    return {'hold': False}\n")
    # B1 fixtures: made-up sidecars
    for i, (label, ctx) in enumerate([("auto-author-demo-widget-s2-r1", 8192), ("gate-demo-widget-s3", 16384),
                                      ("plain-label", 4096)]):
        rec = json.dumps({"id": "%012x" % i, "label": label, "host_pref": "studio", "model": "demo-model:1b", "num_ctx": ctx,
                          "bundle": "demo-widget" if "widget" in label else None, "persisted_at": "2026-01-0%dT00:00:00" % (i + 1)})
        # log rotation moves older jobs into archive/: the last one lives only there, the first is in both (counted once)
        w(os.path.join(st, "ollama-queue-logs", *(("archive",) if i == 2 else ()), "%012x.done.json" % i), rec)
        if i == 0:
            w(os.path.join(st, "ollama-queue-logs", "archive", "%012x.done.json" % i), rec)
    rc, res, _ = run(t, "B1", ["--old-bin", os.path.join(HERE)])
    p = res["parts"][0] if res else {}
    check("B1: same code old and new, all matched, no differences (archive/ read, duplicates once)", rc == 0 and p.get("cases") == 3 and p.get("matched") == 3
          and p.get("differences") == [])
    check("B1: lane, model, priority are reported as not comparable with reasons",
          any(k.startswith("lane") for k in p.get("not_comparable", {})) and any(k.startswith("priority") for k in p.get("not_comparable", {})))
    rc, res, _ = run(t, "B1", ["--old-bin", old])
    p = res["parts"][0] if res else {}
    check("B1: planted old/new difference caught", rc == 1 and any(d["field"] in ("bundle", "ctx_ceiling") for d in p.get("differences", [])))
    w(os.path.join(st, "ollama-queue-logs", "%012x.done.json" % 9),
      json.dumps({"id": "%012x" % 9, "label": "big", "host_pref": "studio", "model": "demo-model:1b", "num_ctx": 10 ** 8,
                  "persisted_at": "2026-01-09T00:00:00"}))
    rc, res, _ = run(t, "B1", [])
    p = res["parts"][0]
    check("B1: planted recorded window above the ceiling caught", any(d["field"] == "num_ctx<=ceiling" for d in p["differences"]))
    # an Unraid job that asked for more than the worker's cap ran at the cap: compared at the cap, not flagged
    w(os.path.join(st, "ollama-queue-logs", "%012x.done.json" % 10),
      json.dumps({"id": "%012x" % 10, "label": "unraid-big", "host_pref": "unraid", "model": "qwen3.5:9b",
                  "num_ctx": 49152, "persisted_at": "2026-01-10T00:00:00"}))
    rc, res, _ = run(t, "B1", [])
    p = res["parts"][0]
    check("B1: Unraid window above the worker cap is compared at the cap (not a difference)",
          not any(d["case"] == "%012x" % 10 for d in p["differences"]))
    # a Darkbloom-lane job: the window comes from live memory, so it is not comparable on a copy
    rc, res, _ = run(t, "B1", [], env_extra={"DARKBLOOM_BASE_URL": "http://127.0.0.1:9"})
    p = res["parts"][0]
    check("B1: Darkbloom-lane windows are not comparable, with the reason",
          not any(d["field"] == "num_ctx<=ceiling" for d in p["differences"])
          and any(k.startswith("Darkbloom window") for k in p.get("not_comparable", {})))
    # B2 fixtures: made-up holds
    rows = []
    for s, e in ((1000.0, 1500.0), (3000.0, 3200.0), (5000.0, 5005.0)):
        rows.append({"kind": "sandbox-hold", "ts": s, "lease_s": 60})
        rows.append({"kind": "sandbox-release", "ts": e, "held_for_s": int(e - s)})
    w(os.path.join(disp, "infra-incidents.jsonl"), "".join(json.dumps(r) + "\n" for r in rows))
    rc, res, _ = run(t, "B2", ["--old-bin", HERE])
    p = res["parts"][0] if res else {}
    check("B2: old and new paths match the recorded holds (short hold skipped, said so)",
          rc == 0 and p.get("cases") == 2 and p.get("matched") == 2 and p.get("differences") == []
          and any("shorter" in k for k in p.get("not_comparable", {})))
    rc, res, _ = run(t, "B2", ["--old-bin", old])
    p = res["parts"][0] if res else {}
    check("B2: planted old-path difference caught", rc == 1 and len(p.get("differences", [])) > 0)
    # B3
    base = os.path.join(cases, "b3base")
    w(os.path.join(base, "v.txt"), "one\n")
    w(os.path.join(cases, "good.diff"), "--- a/v.txt\n+++ b/v.txt\n@@ -1 +1 @@\n-one\n+two\n")
    w(os.path.join(cases, "bad.diff"), "--- a/v.txt\n+++ b/v.txt\n@@ -1 +1 @@\n-one\n+three\n")
    w(os.path.join(cases, "b3.jsonl"),
      json.dumps({"id": "c1", "base": "b3base", "diff": "good.diff", "check": "grep -q two v.txt", "recorded": "pass"}) + "\n" +
      json.dumps({"id": "c2", "base": "b3base", "diff": "bad.diff", "check": "grep -q two v.txt", "recorded": "fail"}) + "\n" +
      json.dumps({"id": "c3", "base": "b3base", "diff": "bad.diff", "check": "grep -q two v.txt", "recorded": "pass"}) + "\n")
    rc, res, _ = run(t, "B3")
    p = res["parts"][0] if res else {}
    check("B3: two matching verdicts, planted wrong record caught", p.get("cases") == 3 and p.get("matched") == 2
          and [d["case"] for d in p.get("differences", [])] == ["c3"])
    check("B3: base tree untouched", open(os.path.join(base, "v.txt")).read() == "one\n")
    # B4 / B5
    w(os.path.join(cases, "job1", "run.py"),
      "import json,subprocess,sys\nr=subprocess.run(['git','push'],capture_output=True)\n"
      "json.dump({'verdict':'pass','pushed_rc':r.returncode,'nodes':[1,2],'edges':[1],'files':['a.py']},open('out.json','w'))\n"
      "sys.exit(0)\n")
    w(os.path.join(cases, "b4.jsonl"),
      json.dumps({"id": "j1", "cwd": "job1", "argv": [sys.executable, "run.py"], "result_file": "out.json",
                  "expect": {"exit": 0, "verdict": "pass", "result": {"pushed_rc": 0}}}) + "\n" +
      json.dumps({"id": "j2", "cwd": "job1", "argv": [sys.executable, "run.py"], "result_file": "out.json",
                  "expect": {"exit": 0, "verdict": "fail"}}) + "\n")
    w(os.path.join(cases, "b5.jsonl"),
      json.dumps({"id": "g1", "cwd": "job1", "argv": [sys.executable, "run.py"], "result_file": "out.json",
                  "expect": {"exit": 0, "nodes": 2, "edges": 1, "files": ["a.py"]}}) + "\n" +
      json.dumps({"id": "g2", "cwd": "job1", "argv": [sys.executable, "run.py"], "result_file": "out.json",
                  "expect": {"exit": 0, "nodes": 3, "edges": 1, "files": ["a.py"]}}) + "\n")
    rc, res, _ = run(t, "B4,B5")
    p4, p5 = res["parts"] if res else ({}, {})
    check("B4: matching re-run, planted verdict difference caught", p4.get("matched") == 1 and [d["case"] for d in p4["differences"]] == ["j2"])
    check("B5: matching graph, planted node-count difference caught", p5.get("matched") == 1 and [d["case"] for d in p5["differences"]] == ["g2"])
    check("B4: git push inside a re-run hit the blocking shim (rc 0; a real push outside a repo gives 128)", p4.get("matched") == 1)
    # B6: stub API
    docs = {"/api/a": {"jobs": 3, "updated_at": 5, "name": "x"}, "/api/b": {"n": 1}}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.command != "GET" or self.path not in docs:
                self.send_response(404); self.end_headers(); return
            b = json.dumps(docs[self.path]).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d" % srv.server_address[1]
    w(os.path.join(cases, "b6", "a.json"), json.dumps({"jobs": 3, "updated_at": 999, "name": "x"}))
    w(os.path.join(cases, "b6", "b.json"), json.dumps({"n": 2}))
    w(os.path.join(cases, "b6", "routes.json"), json.dumps([{"path": "/api/a", "mac_file": "a.json"}, {"path": "/api/b", "mac_file": "b.json"}]))
    rc, res, _ = run(t, "B6", ["--api-url", url])
    p = res["parts"][0] if res else {}
    check("B6: timestamp-only difference ignored, planted value difference caught",
          p.get("matched") == 1 and [d["case"] for d in p["differences"]] == ["/api/b"])
    srv.shutdown()
    # B7
    evs = [{"seq": i, "kind": "done", "text": "job %d finished" % i} for i in range(1, 6)]
    w(os.path.join(cases, "b7", "source", "queue-events.jsonl"), "".join(json.dumps(e) + "\n" for e in evs))
    w(os.path.join(cases, "b7", "source", "ESCALATIONS.md"), "# esc\n- one\n")
    w(os.path.join(cases, "b7", "mac", "queue-events.jsonl"), "".join(json.dumps(e) + "\n" for e in evs))
    w(os.path.join(cases, "b7", "mac", "ESCALATIONS.md"), "# esc\n- one\n")
    rc, res, _ = run(t, "B7")
    p = res["parts"][0] if res else {}
    check("B7: mirror equals the Mac files", rc == 0 and p.get("cases") == 2 and p.get("matched") == 2)
    evs[2]["text"] = "job 3 failed"
    w(os.path.join(cases, "b7", "mac", "queue-events.jsonl"), "".join(json.dumps(e) + "\n" for e in evs))
    rc, res, _ = run(t, "B7")
    p = res["parts"][0] if res else {}
    check("B7: planted event text difference caught at its seq", rc == 1 and p["differences"] and p["differences"][0]["case"] == "seq 3")
    # whole run: status files, table, scratch gone, state untouched
    before = sorted(os.listdir(os.path.join(st, "ollama-queue-logs")))
    rc, res, out = run(t, "B1,B7", ["--old-bin", HERE])
    st_json = json.load(open(os.path.join(out, "status.json")))
    check("run writes status.json (done) and a table; scratch removed", st_json["state"] == "done" and "B1 decisions" in res["table"]
          and not os.path.exists(os.path.join(out, "scratch")))
    check("state dir records untouched", before == sorted(os.listdir(os.path.join(st, "ollama-queue-logs"))))
    p = subprocess.run([sys.executable, os.path.join(HERE, "queue-backtest"), "run", "--out", os.path.join(st, "elsewhere")],
                       capture_output=True, text=True, env=dict(os.environ, QUEUE_STATE_DIR=st, QUEUE_DISPATCH_DIR=disp,
                                                                HOME=os.path.join(t, "home"),
                                                                QUEUE_BACKTEST_DIR=os.path.join(st, ".backtest")))
    check("--out inside the state dir (outside .backtest) is refused", p.returncode == 2)
print("%d failed" % bad)
sys.exit(1 if bad else 0)
