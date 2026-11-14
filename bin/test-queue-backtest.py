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
    tooldir = os.path.join(t, "tool-here")
    w(os.path.join(base, "v.txt"), "one\n")
    w(os.path.join(cases, "good.diff"), "--- a/v.txt\n+++ b/v.txt\n@@ -1 +1 @@\n-one\n+two\n")
    w(os.path.join(cases, "bad.diff"), "--- a/v.txt\n+++ b/v.txt\n@@ -1 +1 @@\n-one\n+three\n")
    w(os.path.join(cases, "b3.jsonl"),
      json.dumps({"id": "c1", "base": "b3base", "diff": "good.diff", "check": "grep -q two v.txt", "recorded": "pass"}) + "\n" +
      json.dumps({"id": "c2", "base": "b3base", "diff": "bad.diff", "check": "grep -q two v.txt", "recorded": "fail"}) + "\n" +
      json.dumps({"id": "c3", "base": "b3base", "diff": "bad.diff", "check": "grep -q two v.txt", "recorded": "pass"}) + "\n" +
      # the check sees a git repo whose HEAD is the base: only the diff's file changed, the helper stays untracked
      json.dumps({"id": "c4", "base": "b3base", "diff": "good.diff", "untracked": ["h.txt"], "recorded": "pass",
                  "check": "test \"$(git diff --name-only HEAD)\" = v.txt && git status --porcelain | grep -qx '?? h.txt'"
                           " && test \"$(git log --format=%s)\" = base"}) + "\n" +
      # no check verdict on the Mac (job stopped first): not comparable, with the case's own reason
      json.dumps({"id": "c5", "base": "b3base", "diff": "good.diff", "check": "true", "recorded": "unknown",
                  "why": "Mac job force-stopped before its check"}) + "\n" +
      # the Mac's absolute tool path, in the base tree and in the diff, mapped to where the tool lives here
      json.dumps({"id": "c6", "base": "b3base", "diff": "tool.diff", "check": "sh run.sh && sh run2.sh", "recorded": "pass",
                  "path_map": {"/nonexistent-mac/bin/tool": tooldir}}) + "\n" +
      # a differing verdict carries the end of the check's output
      json.dumps({"id": "c7", "base": "b3base", "diff": "good.diff", "check": "echo b3-tail-marker; exit 3",
                  "recorded": "pass"}) + "\n")
    w(os.path.join(base, "h.txt"), "helper\n")
    w(os.path.join(base, "run.sh"), "sh /nonexistent-mac/bin/tool/ok.sh\n")
    w(os.path.join(cases, "tool.diff"), "--- /dev/null\n+++ b/run2.sh\n@@ -0,0 +1 @@\n+sh /nonexistent-mac/bin/tool/ok.sh\n")
    w(os.path.join(tooldir, "ok.sh"), "exit 0\n")
    rc, res, _ = run(t, "B3")
    p = res["parts"][0] if res else {}
    check("B3: matching verdicts, planted wrong records caught", p.get("cases") == 7 and p.get("matched") == 4
          and [d["case"] for d in p.get("differences", [])] == ["c3", "c7"])
    check("B3: no recorded verdict is not comparable, with its reason",
          (p.get("not_comparable") or {}).get("Mac job force-stopped before its check") == 1)
    check("B3: Mac tool path mapped in the base tree and the diff (case c6 matched)",
          "c6" not in [d["case"] for d in p.get("differences", [])]
          and "/nonexistent-mac/" in open(os.path.join(base, "run.sh")).read()
          and "/nonexistent-mac/" in open(os.path.join(cases, "tool.diff")).read())
    check("B3: a difference shows the end of the check's output",
          any(d["case"] == "c7" and "b3-tail-marker" in d.get("tail", "") for d in p.get("differences", [])))
    check("B3: base tree untouched (no .git left in it)", open(os.path.join(base, "v.txt")).read() == "one\n"
          and not os.path.exists(os.path.join(base, ".git")))
    # B4 / B5
    w(os.path.join(cases, "job1", "run.py"),
      "import json,subprocess,sys\nr=subprocess.run(['git','push'],capture_output=True)\n"
      "json.dump({'verdict':'pass','pushed_rc':r.returncode,'nodes':[1,2],'edges':[1],'files':['a.py'],"
      "'info':{'mac_nodes_not_rebuilt':3}},open('out.json','w'))\n"
      "sys.exit(0)\n")
    w(os.path.join(cases, "job2", "run.py"),
      "import json,os,sys\nprint('b45-tail-marker')\n"
      "if os.environ.get('B45_BUSY'):\n json.dump({'not_comparable':'Darkbloom busy'},open('out.json','w')); sys.exit(3)\n"
      "json.dump({'verdict':'FAIL','raw_verdict':'FAIL (gates) class=MODEL','seen':os.environ.get('B45_TOKEN_NAME','-'),"
      "'leak':os.environ.get('B45_NOT_PASSED','-')},open('out.json','w'))\nsys.exit(1)\n")
    os.environ["B45_TOKEN_NAME"] = "tok-from-env"
    os.environ["B45_NOT_PASSED"] = "secret"
    w(os.path.join(cases, "b4.jsonl"),
      json.dumps({"id": "j1", "cwd": "job1", "argv": [sys.executable, "run.py"], "result_file": "out.json",
                  "expect": {"exit": 0, "verdict": "pass", "result": {"pushed_rc": 0}}}) + "\n" +
      json.dumps({"id": "j2", "cwd": "job1", "argv": [sys.executable, "run.py"], "result_file": "out.json",
                  "expect": {"exit": 0, "verdict": "fail"}}) + "\n" +
      json.dumps({"id": "j3", "cwd": "job2", "argv": [sys.executable, "run.py"], "result_file": "out.json",
                  "env_pass": ["B45_TOKEN_NAME", "B45_BUSY"],
                  "expect": {"exit": 1, "verdict": "PASS", "result": {"seen": "tok-from-env", "leak": "-"}}}) + "\n")
    w(os.path.join(cases, "b5.jsonl"),
      json.dumps({"id": "g1", "cwd": "job1", "argv": [sys.executable, "run.py"], "result_file": "out.json",
                  "expect": {"exit": 0, "nodes": 2, "edges": 1, "files": ["a.py"]}}) + "\n" +
      json.dumps({"id": "g2", "cwd": "job1", "argv": [sys.executable, "run.py"], "result_file": "out.json",
                  "expect": {"exit": 0, "nodes": 3, "edges": 1, "files": ["a.py"]}}) + "\n" +
      json.dumps({"id": "g3", "cwd": "job1", "argv": [sys.executable, "run.py"], "result_file": "out.json", "edges_tol": 0.5,
                  "expect": {"exit": 0, "nodes": 2, "edges": 2, "files": ["a.py"]}}) + "\n" +
      json.dumps({"id": "g4", "cwd": "job1", "argv": [sys.executable, "run.py"], "result_file": "out.json", "nodes_tol": 0.1,
                  "expect": {"exit": 0, "nodes": 3, "edges": 1, "files": ["a.py"]}}) + "\n")
    rc, res, _ = run(t, "B4,B5")
    p4, p5 = res["parts"] if res else ({}, {})
    check("B4: matching re-run, planted verdict differences caught", p4.get("matched") == 1
          and [d["case"] for d in p4["differences"]] == ["j2", "j3"])
    d3 = [d for d in p4["differences"] if d["case"] == "j3"]
    check("B4: a verdict difference carries the raw verdict and the end of the run's output",
          d3 and d3[0].get("detail") == "FAIL (gates) class=MODEL" and "b45-tail-marker" in d3[0].get("tail", ""))
    check("B4: env_pass hands the case only the variables it names (token read from the environment, other one withheld)",
          not [d for d in p4["differences"] if d["case"] == "j3" and d["field"] in ("seen", "leak")])
    rc, res, _ = run(t, "B4", env_extra={"B45_BUSY": "1"})
    pb = res["parts"][0] if res else {}
    check("B4: a not-comparable result is counted with its reason, not as a match or a difference",
          pb.get("not_comparable") == {"Darkbloom busy": 1} and pb.get("matched") == 1
          and [d["case"] for d in pb["differences"]] == ["j2"])
    check("B5: matching graph, planted node-count differences caught (g2 exact, g4 outside its tolerance)",
          p5.get("matched") == 2 and [d["case"] for d in p5["differences"]] == ["g2", "g4"])
    check("B5: an edge count inside the case's tolerance matches and is listed as tolerated",
          [(t["case"], t["field"], t["expected"], t["got"]) for t in p5.get("tolerated", [])] == [("g3", "edges", 2, 1)])
    check("B5: a run's info block is reported per case, outside the grading",
          [(i["case"], i["info"]) for i in p5.get("info", [])] == [(g, {"mac_nodes_not_rebuilt": 3})
                                                                   for g in ("g1", "g2", "g3", "g4")])
    check("B4: git push inside a re-run hit the blocking shim (rc 0; a real push outside a repo gives 128)", p4.get("matched") == 1)
    # B6: stub API
    docs = {"/api/a": {"jobs": 3, "updated_at": 5, "name": "x", "r": [{"seen_s_ago": 1.5}]}, "/api/b": {"n": 1},
            "/api/c": {"cfg": {"path": "/config/hosts.json", "n": 1}}, "/api/d": {"cfg": {"path": "/x", "n": 2}},
            "/api/e": [{"id": "b", "timestamp": 7}, {"id": "a", "timestamp": 7}, {"id": "c", "timestamp": 5}],
            "/api/f": [{"id": "c", "timestamp": 5}, {"id": "a", "timestamp": 7}, {"id": "b", "timestamp": 7}],
            "/api/g": {"unattached": ["y", "x"], "n": 1},
            "/api/h": [{"row": {"id": "q", "timestamp": 3}, "children": []},
                       {"row": {"id": "p", "timestamp": 3}, "children": []}],
            "/api/i": {"runners": [{"id": "r1", "n": 1}, {"id": "r2", "n": 2}]},
            "/api/j": {"runners": [{"id": "r1", "n": 1}, {"id": "r2", "n": 5}]},
            "/api/k": [{"id": "a", "timestamp": 9}, {"id": "b", "timestamp": 5}, {"id": "d", "timestamp": 5}],
            "/api/l": [{"id": "a", "timestamp": 9}, {"id": "b", "timestamp": 5}, {"id": "d", "timestamp": 5}]}

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
    w(os.path.join(cases, "b6", "a.json"), json.dumps({"jobs": 3, "updated_at": 999, "name": "x", "r": [{"seen_s_ago": 40.0}]}))
    w(os.path.join(cases, "b6", "b.json"), json.dumps({"n": 2}))
    w(os.path.join(cases, "b6", "c.json"), json.dumps({"cfg": {"path": "/Users/x/hosts.json", "n": 1}}))
    w(os.path.join(cases, "b6", "d.json"), json.dumps({"cfg": {"path": "/y", "n": 3}}))
    tie = [{"list": "", "key": "/timestamp", "by": "/id"}]
    w(os.path.join(cases, "b6", "e.json"), json.dumps([{"id": "a", "timestamp": 7}, {"id": "b", "timestamp": 7},
                                                       {"id": "c", "timestamp": 5}]))
    w(os.path.join(cases, "b6", "f.json"), json.dumps([{"id": "a", "timestamp": 7}, {"id": "b", "timestamp": 7},
                                                       {"id": "c", "timestamp": 5}]))
    w(os.path.join(cases, "b6", "g.json"), json.dumps({"unattached": ["x", "y"], "n": 1}))
    w(os.path.join(cases, "b6", "h.json"), json.dumps([{"row": {"id": "p", "timestamp": 3}, "children": []},
                                                       {"row": {"id": "q", "timestamp": 3}, "children": []}]))
    runners = {"runners": [{"id": "r1", "n": 1, "caps": {"node": 26}, "disk": {"free": 9}},
                           {"id": "r2", "n": 2, "caps": {"node": 24}}]}
    w(os.path.join(cases, "b6", "i.json"), json.dumps(runners))
    w(os.path.join(cases, "b6", "j.json"), json.dumps(runners))
    each = ["/runners/*/caps", "/runners/*/disk"]
    cut = [{"id": "a", "timestamp": 9}, {"id": "c", "timestamp": 5}, {"id": "b", "timestamp": 5}]
    w(os.path.join(cases, "b6", "k.json"), json.dumps(cut))
    w(os.path.join(cases, "b6", "l.json"), json.dumps(cut))
    w(os.path.join(cases, "b6", "routes.json"), json.dumps([
        {"path": "/api/a", "mac_file": "a.json"}, {"path": "/api/b", "mac_file": "b.json"},
        {"path": "/api/c", "mac_file": "c.json", "ignore": ["/cfg/path"]},
        {"path": "/api/d", "mac_file": "d.json", "ignore": ["/cfg/path"]},
        {"path": "/api/e", "mac_file": "e.json", "ties": tie}, {"path": "/api/f", "mac_file": "f.json", "ties": tie},
        {"path": "/api/g", "mac_file": "g.json", "unordered": ["/unattached"]},
        {"path": "/api/h", "mac_file": "h.json", "ties": [{"list": "", "key": "/row/timestamp", "by": "/row/id"}]},
        {"path": "/api/i", "mac_file": "i.json", "ignore": each}, {"path": "/api/j", "mac_file": "j.json", "ignore": each},
        {"path": "/api/k", "mac_file": "k.json", "ties": [dict(tie[0], limit=3)]},
        {"path": "/api/l", "mac_file": "l.json", "ties": [dict(tie[0], limit=4)]}]))
    rc, res, _ = run(t, "B6", ["--api-url", url])
    p = res["parts"][0] if res else {}
    check("B6: timestamp and *_ago differences ignored, planted value difference caught",
          [d["case"] for d in p.get("differences", [])][:1] == ["/api/b"])
    check("B6: an ignored key path is skipped, a value beside it still compared",
          [d["case"] for d in p.get("differences", [])][:2] == ["/api/b", "/api/d"])
    check("B6: same-timestamp ties in another order match (flat and nested key), a real order difference is caught",
          (p.get("matched"), [d["case"] for d in p.get("differences", [])]) == (7, ["/api/b", "/api/d", "/api/f", "/api/j", "/api/l"]))
    check("B6: an unordered list matches in any order",
          "/api/g" not in [d["case"] for d in p.get("differences", [])] and p.get("cases") == 12)
    check("B6: an ignore path with * drops the key from every list item, a value beside it still compared",
          "/api/i" not in [d["case"] for d in p.get("differences", [])]
          and [d.get("field") for d in p.get("differences", []) if d["case"] == "/api/j"] == ["/runners[1]/n"])
    check("B6: a tie run cut off at the route's limit compares by count and key only; under the limit it is compared",
          "/api/k" not in [d["case"] for d in p.get("differences", [])]
          and [d.get("field") for d in p.get("differences", []) if d["case"] == "/api/l"] == ["[2]/id"])
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
    evs[2]["text"] = "job 3 finished"
    pre = [{"kind": "done", "text": "before numbering %d" % i} for i in range(2)]
    for side in ("source", "mac"):
        w(os.path.join(cases, "b7", side, "queue-events.jsonl"), "".join(json.dumps(e) + "\n" for e in pre + evs))
    rc, res, _ = run(t, "B7")
    p = res["parts"][0] if res else {}
    check("B7: lines from before events had a seq are left out and counted in the note",
          rc == 0 and p.get("matched") == 2 and p.get("note", "").startswith("2 queue-events.jsonl lines have no seq"))
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
