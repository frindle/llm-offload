#!/usr/bin/env python3
"""Tests for prompt jobs (2026-11-16): prompt_jobs.py (the /api/prompt-jobs rules), prompt-json-runner.py (one prompt,
JSON back) and the daemon's routing for them (free lanes only, loaded Darkbloom model, no page, no drift).

Temp dirs, a fake queue lock and an injected model call; no network, no queue.
  python3 test-prompt-jobs.py      prints ALL PASS / N FAILED (rc 0 / 1)
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import prompt_jobs as P  # noqa: E402

FAILS, N = [], [0]
TOK = "t" * 32
AUTH = {"Authorization": "Bearer " + TOK}
SECRET = "Order 4471 for ZEBRA-MARKER-9 total 12.00"


def chk(name, cond, detail=""):
    N[0] += 1
    if not cond:
        FAILS.append(name)
        print("FAIL", name, detail)


def load(name, file):
    loader = SourceFileLoader(name, str(BIN / file))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


class FakeQ:
    """The bits of ollama-queue.py prompt_jobs uses: _Locked (load/save), cancel_job, _lane_name."""

    def __init__(self):
        self.state = {"jobs": []}
        self.cancelled = []

    def _Locked(self):
        outer = self

        class L:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def load(self):
                return outer.state

            def save(self, st):
                outer.state = st
        return L()

    def cancel_job(self, jid, explicit=False):
        self.cancelled.append(jid)
        self.state["jobs"] = [j for j in self.state["jobs"] if j["id"] != jid]

    def _lane_name(self, lane):
        return {"http://db:8000": "studio-db"}.get(lane, lane)

    def row(self, jid):
        return next(j for j in self.state["jobs"] if j["id"] == jid)


def body(**kw):
    d = {"app": "resell-tracker", "ref": "rt-1", "system": "Extract the order.", "text": SECRET}
    d.update(kw)
    return json.dumps(d).encode()


def t_auth():
    os.environ.pop("QUEUE_PROMPT_TOKEN", None)
    chk("no token -> 503 off", P.handle(FakeQ(), "GET", "/api/prompt-jobs/abc", AUTH)[0] == 503)
    os.environ["QUEUE_PROMPT_TOKEN"] = "short"
    chk("short token counts as none", P.token() == "")
    os.environ["QUEUE_PROMPT_TOKEN"] = TOK
    chk("wrong token -> 401", P.handle(FakeQ(), "GET", "/api/prompt-jobs/abc", {"Authorization": "Bearer x"})[0] == 401)
    chk("no header -> 401", P.handle(FakeQ(), "GET", "/api/prompt-jobs/abc", {})[0] == 401)
    chk("right token passes", P.auth(AUTH) is None)
    d = Path(tempfile.mkdtemp(prefix="pjt-"))
    os.environ.pop("QUEUE_PROMPT_TOKEN")
    (d / "tok").write_text(TOK + "\n")
    os.environ["QUEUE_PROMPT_TOKEN_FILE"] = str(d / "tok")
    chk("token file read", P.token() == TOK)
    os.environ.pop("QUEUE_PROMPT_TOKEN_FILE")
    os.environ["QUEUE_PROMPT_TOKEN"] = TOK
    shutil.rmtree(d)


def t_validate():
    q, base = FakeQ(), Path(tempfile.mkdtemp(prefix="pjv-"))
    h = lambda b: P.handle(q, "POST", "/api/prompt-jobs", AUTH, b, base=base)
    chk("bad app", h(body(app="Resell Tracker"))[0] == 400)
    chk("bad ref", h(body(ref="has space"))[0] == 400)
    chk("ref too long", h(body(ref="r" * 65))[0] == 400)
    chk("no text", h(body(text=""))[0] == 400)
    chk("text not a string", h(body(text=5))[0] == 400)
    chk("not JSON", h(b"{nope")[0] == 400)
    chk("not an object", h(b"[1]")[0] == 400)
    chk("text too big", h(body(text="x" * (P.MAX_TEXT + 1)))[0] == 413)
    chk("system too big", h(body(system="x" * (P.MAX_SYSTEM + 1)))[0] == 413)
    chk("body too big", h(b" " * (P.MAX_BODY + 1))[0] == 413)
    chk("GET on the collection -> 405", P.handle(q, "GET", "/api/prompt-jobs", AUTH, base=base)[0] == 405)
    chk("nothing queued by bad requests", q.state["jobs"] == [])
    shutil.rmtree(base)


def t_create():
    q, base = FakeQ(), Path(tempfile.mkdtemp(prefix="pjc-"))
    st, out = P.handle(q, "POST", "/api/prompt-jobs", AUTH, body(), base=base)
    chk("created 202", st == 202 and out["status"] == "pending", (st, out))
    jid = out["id"]
    row = q.row(jid)
    chk("row: prompt host pref + runner", row["host_pref"] == "prompt" and row["runner"].endswith("prompt-json-runner.py"))
    chk("row: research kind, fit pass skipped", row["task_kind"] == "research" and row["fit_checked"] is True)
    chk("row: label has no ref", row["label"] == "prompt-resell-tracker-" + jid)
    import docsum_private
    chk("row: label is private (kept off handoff lists, auto-clear digests and follow-ups)",
        docsum_private.is_private_label(row["label"]) and not docsum_private.is_docsum_label(row["label"]))
    chk("row: model from PROMPT_JOB_MODEL default", row["model"] == "qwen3:8b")
    d = base / jid
    chk("dir 0700", stat.S_IMODE(d.stat().st_mode) == 0o700)
    chk("task 0600", stat.S_IMODE((d / "task.json").stat().st_mode) == 0o600)
    task = json.loads((d / "task.json").read_text())
    chk("task holds the text", task["text"] == SECRET and task["json"] is True and task["max_tokens"] == 2048)
    chk("meta has no text", SECRET not in (d / "meta.json").read_text())
    chk("row has no text", SECRET not in json.dumps(row))
    st2, out2 = P.handle(q, "POST", "/api/prompt-jobs", AUTH, body(), base=base)
    chk("same app+ref while live -> 200 same id", st2 == 200 and out2["id"] == jid, (st2, out2))
    chk("still one row", len(q.state["jobs"]) == 1)
    for i in range(2, 21):
        P.handle(q, "POST", "/api/prompt-jobs", AUTH, body(ref="rt-%d" % i), base=base)
    st3, out3 = P.handle(q, "POST", "/api/prompt-jobs", AUTH, body(ref="rt-99"), base=base)
    chk("21st live job -> 429", st3 == 429 and out3["error"] == "E_BUSY", (st3, out3))
    st4, _ = P.handle(q, "POST", "/api/prompt-jobs", AUTH, body(app="bloom-ledger", ref="rt-99"), base=base)
    chk("another app has its own limit", st4 == 202)
    q.row(jid)["status"] = "done"
    st5, out5 = P.handle(q, "POST", "/api/prompt-jobs", AUTH, body(ref="rt-99"), base=base)
    chk("a finished job frees a place", st5 == 202, (st5, out5))
    (base / jid / "result.json").write_text("{}")
    q.row(out5["id"])["status"] = "done"          # back under the limit first
    st6, out6 = P.handle(q, "POST", "/api/prompt-jobs", AUTH, body(), base=base)
    chk("same ref after it finished -> a new job", st6 == 202 and out6["id"] != jid)
    shutil.rmtree(base)


def t_view_get_delete():
    v = P.view
    chk("pending", v("a", {}, {"status": "pending"}, None)["status"] == "pending")
    chk("held counts as pending", v("a", {}, {"status": "held"}, None)["status"] == "pending")
    r = v("a", {}, {"status": "running", "model": "m1"}, None, "studio-db")
    chk("running on darkbloom", r["status"] == "running" and r["lane"] == "darkbloom" and r["model"] == "m1")
    r = v("a", {}, {"status": "done"}, {"ok": True, "result": {"x": 1}, "model": "m2", "lane": "ollama"}, "unraid")
    chk("done with result", r == {"id": "a", "status": "done", "lane": "unraid", "model": "m2", "result": {"x": 1},
                                  "error": None}, r)
    r = v("a", {}, {"status": "done"}, {"ok": False, "result": None, "error": "E_NOT_JSON"})
    chk("failed parse", r["status"] == "failed" and r["error"] == "E_NOT_JSON" and r["result"] is None)
    chk("row gone, no result -> E_LOST", v("a", {}, None, None)["error"] == "E_LOST")
    chk("done, no result -> E_NO_RESULT", v("a", {}, {"status": "done"}, None)["error"] == "E_NO_RESULT")
    chk("queue failed -> E_QUEUE", v("a", {}, {"status": "failed"}, None)["error"] == "E_QUEUE")
    chk("result kept after the row is purged", v("a", {}, None, {"ok": True, "result": {}})["status"] == "done")
    q, base = FakeQ(), Path(tempfile.mkdtemp(prefix="pjg-"))
    jid = P.handle(q, "POST", "/api/prompt-jobs", AUTH, body(), base=base)[1]["id"]
    q.row(jid)["lane"] = "http://db:8000"
    q.row(jid)["status"] = "running"
    st, out = P.handle(q, "GET", "/api/prompt-jobs/" + jid, AUTH, base=base)
    chk("GET running via the lane name", st == 200 and out["status"] == "running" and out["lane"] == "darkbloom", out)
    chk("DELETE running -> 409", P.handle(q, "DELETE", "/api/prompt-jobs/" + jid, AUTH, base=base)[0] == 409)
    q.row(jid)["status"] = "pending"
    st, _ = P.handle(q, "DELETE", "/api/prompt-jobs/" + jid, AUTH, base=base)
    chk("DELETE pending cancels the row and the dir", st == 200 and q.cancelled == [jid] and not (base / jid).exists())
    chk("GET after delete -> 404", P.handle(q, "GET", "/api/prompt-jobs/" + jid, AUTH, base=base)[0] == 404)
    chk("bad id -> 404", P.handle(q, "GET", "/api/prompt-jobs/../etc", AUTH, base=base)[0] == 404)
    jid2 = P.handle(q, "POST", "/api/prompt-jobs", AUTH, body(ref="rt-2"), base=base)[1]["id"]
    q.row(jid2)["status"] = "done"
    st, _ = P.handle(q, "DELETE", "/api/prompt-jobs/" + jid2, AUTH, base=base)
    chk("DELETE done keeps the queue row as history", st == 200 and q.row(jid2) and q.cancelled == [jid])
    shutil.rmtree(base)


def t_sweep():
    base = Path(tempfile.mkdtemp(prefix="pjs-"))
    for jid, age in (("aaaaaaaaaaaa", 15), ("bbbbbbbbbbbb", 2)):
        (base / jid).mkdir()
        (base / jid / "meta.json").write_text(json.dumps({"created_ts": time.time() - age * 86400}))
    (base / "notajobdir").mkdir()
    chk("sweep removes only old job dirs", P.sweep(base) == 1 and not (base / "aaaaaaaaaaaa").exists()
        and (base / "bbbbbbbbbbbb").exists() and (base / "notajobdir").exists())
    shutil.rmtree(base)


def t_runner():
    R = load("pj_runner", "prompt-json-runner.py")
    os.environ["PROMPT_JOB_RETRY_S"] = "0"

    def go(answers, task=None):
        d = Path(tempfile.mkdtemp(prefix="pjr-"))
        tf = d / "task.json"
        tf.write_text(json.dumps(task if task is not None else {"system": "s", "text": SECRET, "json": True}))
        it = iter(answers)
        seen = []

        def call(sy, us):
            seen.append((sy, us))
            a = next(it)
            if isinstance(a, Exception):
                raise a
            return a
        a = argparse.Namespace(model="m", host="h", num_ctx=1, cwd=str(d), task_file=str(tf))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rec = R.run(a, call=call)
        disk = json.loads((d / "result.json").read_text())
        mode = stat.S_IMODE((d / "result.json").stat().st_mode)
        shutil.rmtree(d)
        return rec, disk, mode, buf.getvalue(), seen

    rec, disk, mode, log, seen = go(['{"order": "4471", "total": 12.0}'])
    chk("runner ok", rec["ok"] and disk["result"] == {"order": "4471", "total": 12.0} and disk == rec, disk)
    chk("result 0600", mode == 0o600)
    chk("log has no text", SECRET not in log and "ZEBRA" not in log and "4471" not in log, log)
    chk("JSON hint added to the system prompt", seen[0][0].startswith("s") and "JSON object only" in seen[0][0])
    rec = go(['```json\n{"a": 1}\n```'])[0]
    chk("fenced JSON", rec["ok"] and rec["result"] == {"a": 1})
    rec = go(['<think>hmm</think> Here: {"a": 2} done'])[0]
    chk("JSON inside prose", rec["ok"] and rec["result"] == {"a": 2})
    rec, _, _, _, seen = go(["not json", '{"a": 3}'])
    chk("retry once, then ok", rec["ok"] and rec["attempts"] == 2 and "not a valid JSON" in seen[1][1])
    rec = go(["nope", "still nope"])[0]
    chk("twice not JSON -> E_NOT_JSON", rec["error"] == "E_NOT_JSON" and not rec["ok"] and rec["result"] is None)
    rec = go(["[1, 2]", "[3]"])[0]
    chk("a JSON list is not an object", rec["error"] == "E_NOT_JSON")
    rec = go(["", "  "])[0]
    chk("empty -> E_EMPTY", rec["error"] == "E_EMPTY")
    rec = go([urllib.error.URLError("down"), OSError("down")])[0]
    chk("lane down twice -> E_LANE", rec["error"] == "E_LANE")
    rec = go([urllib.error.URLError("blip"), '{"a": 4}'])[0]
    chk("one blip, then ok", rec["ok"] and rec["result"] == {"a": 4})
    rec = go(["plain words"], task={"system": "s", "text": "t", "json": False})[0]
    chk("json false -> text", rec["ok"] and rec["result"] == {"text": "plain words"})
    rec = go([], task={"system": "s"})[0]
    chk("no text -> E_TASK", rec["error"] == "E_TASK")
    chk("parse_object junk", R.parse_object("{bad") is None and R.parse_object(None) is None)


def t_daemon():
    q = load("q_pj", "ollama-queue.py")
    runner = str(Path.home() / "bin" / "prompt-json-runner.py")
    chk("runner allowlisted", runner in q.ALLOWED_RUNNERS)
    pj = {"id": "p1", "host_pref": "prompt", "runner": runner, "model": "qwen3:8b", "status": "pending",
          "num_ctx": 12288, "cwd": "/x", "task_file": "/x/task.json"}
    other = {"id": "o1", "host_pref": "auto", "runner": runner, "model": "qwen3:8b", "status": "pending"}
    chk("prompt job recognised", q._is_prompt_job(pj) and not q._is_prompt_job(other))

    class W:
        KNOWN_OLLAMA_HOSTS = {"unraid": {"url": "http://ur:11434"}, "studio": {"url": "http://st:11434"}}
    q._darkbloom_url = lambda: "http://db:8000"
    chk("lanes: darkbloom then unraid", q._candidate_lanes(pj, W()) == ["http://db:8000", "http://ur:11434"])
    q._darkbloom_url = lambda: None
    chk("no darkbloom configured: unraid only", q._candidate_lanes(pj, W()) == ["http://ur:11434"])
    chk("loaded default preferred", q.prompt_db_model(["x-model", q.DARKBLOOM_DEFAULT_MODEL]) == q.DARKBLOOM_DEFAULT_MODEL)
    chk("else the first loaded", q.prompt_db_model(["x-model", "y"]) == "x-model")
    chk("nothing loaded -> None (lane skipped)", q.prompt_db_model([]) is None)
    q._darkbloom_loaded_ids = lambda: []
    chk("skip reason when nothing loaded", "no loaded model" in (q.prompt_db_lane_wait() or ""))
    q._darkbloom_loaded_ids = lambda: ["x-model"]
    q.darkbloom_launch_wait = lambda dbm, now: None
    chk("ready -> no skip", q.prompt_db_lane_wait() is None)
    q.darkbloom_launch_wait = lambda dbm, now: {"short": "sandbox hold"}
    chk("hold -> skip with reason", q.prompt_db_lane_wait() == "darkbloom: sandbox hold")
    chk("never paged as idle", q.idle_alert_pending([pj, other]) == [other])
    q._is_darkbloom_host = lambda u: u == "http://db:8000"
    cmd = q._build_cmd(pj, "http://db:8000")
    chk("darkbloom: the loaded model, not a mapped one", cmd[cmd.index("--model") + 1] == "x-model", cmd)
    cmd = q._build_cmd(pj, "http://ur:11434")
    chk("ollama: the row's model", cmd[cmd.index("--model") + 1] == "qwen3:8b" and cmd[1] == runner, cmd)
    src = (BIN / "ollama-queue.py").read_text()
    chk("drift alert skips prompt jobs", "lambda j: (not _is_prompt_job(j)" in src)
    chk("per-lane darkbloom check in the launch loop", "_pdw = prompt_db_lane_wait()" in src)


def t_api_wiring():
    src = (BIN / "ollama-queue-api.py").read_text()
    post = src[src.index("    def do_POST(self):"):src.index("    def do_DELETE(self):")]
    chk("POST route before the queue write auth",
        post.index("_is_prompt_jobs_path(self.path)") < post.index("self._require_write_auth()"))
    chk("POST route after the drain gate", post.index("self._drain_gate()") < post.index("_is_prompt_jobs_path"))
    dele = src[src.index("    def do_DELETE(self):"):]
    chk("DELETE route before the queue write auth",
        dele.index("_is_prompt_jobs_path(self.path)") < dele.index("self._require_write_auth()"))
    ns = {}
    start = src.index("def _is_prompt_jobs_path(")
    exec(src[start:src.index("\ndef ", start + 10)], ns)
    f = ns["_is_prompt_jobs_path"]
    chk("path match", f("/api/prompt-jobs") and f("/api/prompt-jobs/abc?x=1") and f("/api/prompt-jobs/"))
    chk("no prefix match", not f("/api/prompt-jobsX") and not f("/api/jobs"))


for t in (t_auth, t_validate, t_create, t_view_get_delete, t_sweep, t_runner, t_daemon, t_api_wiring):
    try:
        t()
    except Exception as e:  # noqa: BLE001
        FAILS.append(t.__name__)
        import traceback
        traceback.print_exc()
        print("FAIL", t.__name__, "raised", type(e).__name__, e)
print("ALL PASS (%d checks)" % N[0] if not FAILS else "%d FAILED of %d" % (len(FAILS), N[0]))
sys.exit(1 if FAILS else 0)
