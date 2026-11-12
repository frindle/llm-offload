#!/usr/bin/env python3
"""Tests for conformance_log.py (design section 3). Sandboxed temp dirs; no network, no models.

  python3 test-conformance-log.py [--mutants|--revert-check]
CONFORMANCE_LOG_SRC=<path> points at a mutated copy (used by --mutants)."""
import copy
import importlib.util
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = os.environ.get("CONFORMANCE_LOG_SRC") or str(HERE / "conformance_log.py")
sys.path.insert(0, str(HERE))
os.environ.pop("CONFORMANCE_LOG", None)
os.environ.pop("CONFORMANCE_CHECK", None)
spec = importlib.util.spec_from_file_location("conformance_log_under_test", SRC)
C = importlib.util.module_from_spec(spec)
sys.modules["conformance_log_under_test"] = C
spec.loader.exec_module(C)

FAILS = []
KEY = "SENTINEL-CONF-KEY-dddddddddddddddddddd"
NOW = 1791955834.6          # 2026-10-14 UTC
DAY = 86400


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(name)
        print(f"FAIL {name} {detail}")
    else:
        print(f"ok   {name}")


def tmp():
    return Path(tempfile.mkdtemp(prefix="conf-"))


MODEL = "qwen3.6-35b-a3b-vl-mtp-mxfp8"
OPENAI_FIELDS = {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 0.0,
                 "repetition_penalty": 1.0, "repeat_penalty": 1.0, "max_tokens": 32768,
                 "chat_template_kwargs": {"enable_thinking": True, "preserve_thinking": True}}
OLLAMA_FIELDS = {"options": {"temperature": 0.6, "top_k": 20, "num_ctx": 8192, "num_predict": 100}, "think": True}


def stub_expected(plan, role, step=0):
    """Deterministic profile stand-in: step 1 raises temperature (a declared ladder step)."""
    if plan.get("api") == "ollama":
        return copy.deepcopy(OLLAMA_FIELDS)
    f = copy.deepcopy(OPENAI_FIELDS)
    if step:
        f["temperature"] = 0.9
    return f


PLAN = {"plan_id": "J-A", "sha": "ab" * 32, "lane": "studio-db", "host": "http://127.0.0.1:8000", "api": "openai",
        "model": MODEL, "num_ctx": 65536, "auth": {"fp": "sha256:" + C.key_fp8(KEY), "rotations": []},
        "profile": {"role": "author"}}
PLAN_OLLAMA = {"plan_id": "J-B", "sha": "cd" * 32, "lane": "unraid", "host": "http://198.51.100.145:11434", "api": "ollama",
               "model": "qwen3:14b", "num_ctx": 8192, "auth": {"fp": None, "rotations": []}, "profile": {"role": "gate"}}


def rec(plan=PLAN, **kw):
    base = dict(now=NOW, job="J", att="A", plan=plan, n=1, role="author", host=plan["host"],
                path="/v1/chat/completions" if plan["api"] == "openai" else "/api/chat", api=plan["api"],
                model=plan["model"], body={"model": plan["model"], "messages": [{"role": "user", "content": "SECRET-PROMPT"}],
                                           **(OPENAI_FIELDS if plan["api"] == "openai" else OLLAMA_FIELDS)},
                key=KEY if plan["lane"] == "studio-db" else None, status=200, ms=120, ptok=10, ctok=5, fin="stop",
                rmodel=plan["model"])
    base.update(kw)
    return C.build_record(full=True, **base)


def verdict(records, plan=PLAN, **kw):
    return C.classify(plan, records, expected_fn=stub_expected, **kw)


def classes(v):
    return {d["class"] for d in v["drift"]}


# ---------------------------------------------------------------------------------------------
def t_build_record():
    r = rec()
    s = json.dumps(r)
    check("rec.no_prompt_no_key", "SECRET-PROMPT" not in s and KEY not in s and "messages" not in s)
    check("rec.kfp_is_fp8", r["kfp"] == C.key_fp8(KEY) and len(r["kfp"]) == 8)
    check("rec.kfp_from_prefixed_fp", C.build_record(now=1, kfp="sha256:1a2b3c4d")["kfp"] == "1a2b3c4d")
    check("rec.fields_wire_shape", r["fields"]["top_k"] == 20 and "model" not in r["fields"] and "messages" not in r["fields"])
    check("rec.psha_plan", r["plan"] == "J-A" and r["psha"] == "abababab")
    b = {"model": "m", "messages": [], "tools": [{"type": "function", "function": {"name": "x"}}],
         "response_format": {"type": "json_schema", "json_schema": {"name": "SECRET-SCHEMA"}}, "api_key": "zzz",
         "extra": {"authorization": "Bearer q", "ok": 1}}
    f = C.wire_fields(b)
    check("rec.tools_hash_only", "tools" not in f and len(f["_tools_sha"]) == 8)
    check("rec.response_format_presence", f["response_format"] == "present" and "SECRET-SCHEMA" not in json.dumps(f))
    check("rec.secret_named_fields_dropped", "api_key" not in f and "authorization" not in f["extra"] and f["extra"]["ok"] == 1)
    check("rec.max_tokens_not_mistaken_for_secret", "max_tokens" in C.wire_fields({"max_tokens": 5}))
    check("rec.compact_has_no_fields", "fields" not in C.build_record(now=1, body={"a": 1}))
    check("rec.fsha_stable_and_sensitive", C.build_record(now=1, body={"a": 1})["fsha"] == C.build_record(now=2, body={"a": 1})["fsha"]
          != C.build_record(now=1, body={"a": 2})["fsha"])
    check("rec.fsha_ignores_messages", C.build_record(now=1, body={"a": 1, "messages": ["x"]})["fsha"]
          == C.build_record(now=1, body={"a": 1, "messages": ["y", "z"]})["fsha"])
    check("rec.deterministic", rec() == rec())
    check("rec.volume_bounds", len(json.dumps(C.build_record(now=NOW, plan=PLAN, body={"a": 1}, host="http://127.0.0.1:8000",
                                                           path="/v1/chat/completions", api="openai", model=MODEL, job="J" * 12,
                                                           att="A" * 10, role="author", n=7, key=KEY, status=200, ms=5321,
                                                           ptok=8123, ctok=412, fin="tool_calls", rmodel=MODEL))) < 450
          and len(json.dumps(rec())) < 2500)


def t_recorder_and_files():
    d = tmp()
    R = C.Recorder(d, clock=lambda: NOW)
    kw = dict(job="J", att="A", plan=PLAN, n=1, role="author", host=PLAN["host"], path="/v1/chat/completions", api="openai",
              model=MODEL, body={"model": MODEL, "messages": [{"content": "SECRET-PROMPT"}], **OPENAI_FIELDS},
              key=KEY, status=200)
    check("rw.write_ok", R.record(**kw) is True)
    R.record(**{**kw, "n": 2})
    kw3 = {**kw, "n": 3, "body": {**kw["body"], "temperature": 0.9}}
    R.record(**kw3)
    R.record(**{**kw, "n": 4, "plan": {**PLAN, "plan_id": "J-B"}})
    files = list(d.glob("*.jsonl"))
    check("rw.one_day_file_named_utc", [f.name for f in files] == ["2026-10-14.jsonl"], str(files))
    check("rw.mode_0600", files[0].stat().st_mode & 0o777 == 0o600)
    lines = files[0].read_text().splitlines()
    rows = [json.loads(x) for x in lines]
    check("rw.one_line_per_request", len(rows) == 4)
    check("rw.first_full_second_compact", "fields" in rows[0] and "fields" not in rows[1])
    check("rw.changed_fields_full_again", "fields" in rows[2] and rows[2]["fields"]["temperature"] == 0.9)
    check("rw.new_plan_full_again", "fields" in rows[3])
    check("rw.no_prompt_no_key_on_disk", "SECRET-PROMPT" not in files[0].read_text() and KEY not in files[0].read_text())
    check("rw.dir_not_world_readable_files", all(f.stat().st_mode & 0o077 == 0 for f in d.iterdir() if f.is_file()))
    # defence in depth: a key literal smuggled into a field is scrubbed and flagged
    R.record(**{**kw, "n": 5, "body": {**kw["body"], "stop": [KEY]}})
    txt = files[0].read_text()
    last = json.loads(txt.splitlines()[-1])
    check("rw.literal_key_scrubbed", KEY not in txt and last.get("redacted") is True)
    # never raises: directory is a regular file
    bad = tmp(); (bad / "blocked").write_text("x")
    R2 = C.Recorder(bad / "blocked" / "sub", clock=lambda: NOW)
    ok = R2.record(**kw)
    check("rw.never_raises_unwritable", ok is False and R2.errors == 1)
    check("rw.never_raises_bad_args", C.Recorder(d).record(plan=object(), host=object()) in (True, False))
    # kill switch
    d2 = tmp()
    off = C.Recorder(d2, env={"CONFORMANCE_LOG": "0"}, clock=lambda: NOW)
    check("rw.kill_switch", off.record(**kw) is False and not list(d2.glob("*")))
    check("rw.kill_switch_values", not C.log_enabled({"CONFORMANCE_LOG": "off"}) and C.log_enabled({}) and C.log_enabled({"CONFORMANCE_LOG": "1"}))
    # size caps
    d3 = tmp()
    big = {**kw, "body": {**kw["body"], "blob": "x" * 6000}}
    R3 = C.Recorder(d3, clock=lambda: NOW)
    R3.record(**big)
    r0 = json.loads(next(d3.glob("*.jsonl")).read_text().splitlines()[0])
    check("cap.big_fields_truncated_keeps_fsha", "fields" not in r0 and r0["fields_truncated"] is True and r0["fsha"])
    old = C.DAY_CAP_BYTES
    C.DAY_CAP_BYTES = 600
    try:
        d4 = tmp()
        R4 = C.Recorder(d4, clock=lambda: NOW)
        res = [R4.record(**{**kw, "n": i}) for i in range(10)]
    finally:
        C.DAY_CAP_BYTES = old
    size = next(d4.glob("*.jsonl")).stat().st_size
    check("cap.day_cap_enforced", size <= 600 and res.count(False) > 0 and R4.dropped > 0, f"size={size} {res}")
    for x in (d, d2, d3, d4, bad):
        shutil.rmtree(x, ignore_errors=True)


def _worker(args):
    d, i = args
    R = C.Recorder(d, clock=lambda: NOW)
    for n in range(150):
        R.record(job=f"J{i}", att="A", plan=PLAN, n=n, role="author", host=PLAN["host"], path="/v1/chat/completions",
                 api="openai", model=MODEL, body={"model": MODEL, **OPENAI_FIELDS}, key=KEY, status=200)


def t_concurrent_append():
    d = tmp()
    with multiprocessing.get_context("fork").Pool(4) as pool:
        pool.map(_worker, [(str(d), i) for i in range(4)])
    lines = next(d.glob("*.jsonl")).read_text().splitlines()
    ok = 0
    for ln in lines:
        try:
            json.loads(ln); ok += 1
        except ValueError:
            pass
    check("conc.no_torn_lines", len(lines) == 600 and ok == 600, f"{len(lines)} lines {ok} valid")
    shutil.rmtree(d, ignore_errors=True)
    d5 = tmp()
    R5 = C.Recorder(d5, clock=lambda: NOW)
    ok = R5.record(job="J", att="A", plan=PLAN, host=PLAN["host"], path="/v1/chat/completions", api="openai",
                   model="m" * 9000, body={"a": 1})
    check("cap.oversize_record_dropped", ok is False and R5.dropped == 1 and not list(d5.glob("*.jsonl")))
    shutil.rmtree(d5, ignore_errors=True)


def t_prune_and_reader():
    d = tmp()
    now = NOW
    for off in (0, 1, 13, 14, 15, 30):
        (d / f"{C._day(now - off * DAY)}.jsonl").write_text('{"x":1}\n' * 3)
    (d / "notes.txt").write_text("keep")
    (d / ".lock").write_text("")
    removed = C.prune(d, now=now, days=14, cap_bytes=10 ** 9)
    left = sorted(p.name for p in d.glob("????-??-??.jsonl"))
    check("prune.retention_14d", len(left) == 4 and C._day(now - 15 * DAY) + ".jsonl" not in left
          and C._day(now - 14 * DAY) + ".jsonl" in left and len(removed) == 2, f"{left} {removed}")
    check("prune.untouched_other_files", (d / "notes.txt").exists() and (d / ".lock").exists())
    removed = C.prune(d, now=now, days=14, cap_bytes=40)       # 24 bytes/file: keep newest only (<=40 -> 1 file)
    left = sorted(p.name for p in d.glob("????-??-??.jsonl"))
    check("prune.total_cap_oldest_first", left == [C._day(now) + ".jsonl"], str(left))
    check("prune.missing_dir_no_raise", C.prune(d / "nope", now=now) == [])
    # reader
    (d / "2026-10-14.jsonl").write_text('{"t": %s, "plan": "P1", "job": "j1", "att": "a1"}\nTORN{{\n'
                                        '[1]\n{"t": %s, "plan": "P2", "job": "j2", "att": "a2"}\n' % (NOW, NOW + 100))
    allr = list(C.iter_records(d))
    check("read.skips_torn_and_nonobject", len(allr) == 2)
    check("read.filter_plan_job_att", [r["plan"] for r in C.iter_records(d, plan="P2")] == ["P2"]
          and [r["job"] for r in C.iter_records(d, job="j1")] == ["j1"] and [r["att"] for r in C.iter_records(d, att="a2")] == ["a2"])
    check("read.time_window", [r["plan"] for r in C.iter_records(d, since=NOW + 50)] == ["P2"]
          and [r["plan"] for r in C.iter_records(d, until=NOW + 50)] == ["P1"])
    (d / "2026-10-10.jsonl").write_text('{"plan": "OLD"}\n')            # no `t`: only the day filter can exclude it
    check("read.day_filter_since", "OLD" not in [r.get("plan") for r in C.iter_records(d, since=NOW - 100)])
    check("read.day_filter_until", "OLD" in [r.get("plan") for r in C.iter_records(d, until=NOW)]
          and "OLD" not in [r.get("plan") for r in C.iter_records(d, since=NOW - 100, until=NOW)])
    check("read.missing_dir_empty", list(C.iter_records(d / "nope")) == [])
    shutil.rmtree(d, ignore_errors=True)


def t_classify_clean_and_each_drift():
    base = [rec(n=1), rec(n=2)]
    v = verdict(base)
    check("cls.clean_ok", v["ok"] is True and v["drift"] == [] and v["n"] == 2, str(v))
    v = verdict([rec(host="http://127.0.0.1:11434")])
    check("cls.host_drift", classes(v) == {"HOST_DRIFT"}, str(v))
    check("cls.host_trailing_slash_and_v1_ok", verdict([rec(host="http://127.0.0.1:8000/v1/")])["ok"])
    check("cls.api_drift_path", "API_DRIFT" in classes(verdict([rec(path="/api/chat")])))
    check("cls.api_drift_api", "API_DRIFT" in classes(verdict([rec(api="ollama")])))
    check("cls.model_drift_req", "MODEL_DRIFT" in classes(verdict([rec(model="qwen3.5-9b")])))
    check("cls.model_drift_casing", "MODEL_DRIFT" in classes(verdict([rec(model=MODEL.upper())])))
    check("cls.model_drift_rmodel", "MODEL_DRIFT" in classes(verdict([rec(rmodel="other-model")])))
    check("cls.rmodel_absent_ok", verdict([rec(rmodel=None)])["ok"])
    wire = copy.deepcopy(OPENAI_FIELDS)
    del wire["top_k"]
    v = verdict([rec(body={"model": MODEL, **wire})])
    check("cls.param_drift_dropped_top_k", classes(v) == {"PARAM_DRIFT"} and "top_k" in v["drift"][0]["detail"], str(v))
    check("cls.param_drift_changed_value", "PARAM_DRIFT" in classes(verdict([rec(body={"model": MODEL, **{**OPENAI_FIELDS, "temperature": 1.5}})])))
    nested = copy.deepcopy(OPENAI_FIELDS); nested["chat_template_kwargs"]["enable_thinking"] = False
    check("cls.param_drift_nested", "PARAM_DRIFT" in classes(verdict([rec(body={"model": MODEL, **nested})])))
    check("cls.param_extra_fields_ok", verdict([rec(body={"model": MODEL, **OPENAI_FIELDS, "stream": True, "stop": ["x"]})])["ok"])
    check("cls.param_override_declared_ok", verdict([rec(body={"model": MODEL, **{**OPENAI_FIELDS, "max_tokens": 64}}, ov=["max_tokens"])])["ok"])
    check("cls.param_override_not_declared_flagged", "PARAM_DRIFT" in classes(verdict([rec(body={"model": MODEL, **{**OPENAI_FIELDS, "max_tokens": 64}})])))
    check("cls.ladder_step_allowed", verdict([rec(body={"model": MODEL, **{**OPENAI_FIELDS, "temperature": 0.9}}, step=1)])["ok"])
    check("cls.ladder_step_wrong_value", "PARAM_DRIFT" in classes(verdict([rec(body={"model": MODEL, **{**OPENAI_FIELDS, "temperature": 0.7}}, step=1)])))
    check("cls.undeclared_step_value_flagged", "PARAM_DRIFT" in classes(verdict([rec(body={"model": MODEL, **{**OPENAI_FIELDS, "temperature": 0.9}})])))
    # KEY
    check("cls.key_drift_null_on_db", "KEY_DRIFT" in classes(verdict([rec(key=None)])))
    check("cls.key_drift_wrong_fp", "KEY_DRIFT" in classes(verdict([rec(key="SENTINEL-OTHER-KEY-xxxxxxxxxxxxxxxx")])))
    pr = copy.deepcopy(PLAN_OLLAMA)
    check("cls.key_drift_on_non_db", "KEY_DRIFT" in classes(C.classify(pr, [rec(plan=pr, key=KEY)], expected_fn=stub_expected)))
    check("cls.ollama_clean", C.classify(pr, [rec(plan=pr, role="gate")], expected_fn=stub_expected)["ok"])
    other = "SENTINEL-ROTATED-KEY-yyyyyyyyyyyyyyyyy"
    rot = [rec(n=1), rec(n=2, key=other, ev="key_rotated"), rec(n=3, key=other)]
    check("cls.key_rotation_event_allows_new_fp", verdict(rot)["ok"], str(verdict(rot)))
    check("cls.new_fp_without_event_flagged", "KEY_DRIFT" in classes(verdict([rec(n=1), rec(n=2, key=other)])))
    pr2 = {**PLAN, "auth": {"fp": PLAN["auth"]["fp"], "rotations": [{"new_fp": "sha256:" + C.key_fp8(other)}]}}
    check("cls.plan_recorded_rotation_allows", C.classify(pr2, [rec(key=other)], expected_fn=stub_expected)["ok"])
    # CTX
    po = copy.deepcopy(PLAN_OLLAMA)
    bad_ctx = {"options": {**OLLAMA_FIELDS["options"], "num_ctx": 2048}, "think": True}
    v = C.classify(po, [rec(plan=po, role="gate", body={"model": po["model"], **bad_ctx})], expected_fn=stub_expected)
    check("cls.ctx_drift", "CTX_DRIFT" in classes(v) and "PARAM_DRIFT" not in classes(v), str(v))
    # GAP
    check("cls.gap_missing_records", classes(verdict([rec(n=1)], expected_calls=3)) == {"GAP"})
    check("cls.gap_zero_records", classes(verdict([], expected_calls=2)) == {"GAP"})
    check("cls.no_gap_when_equal_or_unknown", verdict([rec(n=1)], expected_calls=1)["ok"] and verdict([rec(n=1)])["ok"])
    # compact record whose full twin was lost
    compact = C.build_record(now=NOW, plan=PLAN, host=PLAN["host"], path="/v1/chat/completions", api="openai", model=MODEL,
                             body={"model": MODEL, **OPENAI_FIELDS}, key=KEY, role="author")
    check("cls.compact_without_full_is_gap", "GAP" in classes(verdict([compact])))
    check("cls.compact_with_full_inherits", verdict([rec(n=1), compact])["ok"])
    check("cls.compact_inherit_detects_drift_in_full",
          "PARAM_DRIFT" in classes(verdict([rec(n=1, body={"model": MODEL, **{**OPENAI_FIELDS, "top_p": 0.1}}),
                                            C.build_record(now=NOW, plan=PLAN, host=PLAN["host"], path="/v1/chat/completions",
                                                           api="openai", model=MODEL, role="author", key=KEY,
                                                           body={"model": MODEL, **{**OPENAI_FIELDS, "top_p": 0.1}})])))
    # multiple classes at once, counted
    v = verdict([rec(host="http://x:1", model="m"), rec(host="http://x:1", model="m")])
    check("cls.multi_class_counts", classes(v) == {"HOST_DRIFT", "MODEL_DRIFT"} and all(d["n"] == 2 for d in v["drift"]))
    check("cls.order_stable", [d["class"] for d in v["drift"]] == [c for c in C.DRIFT_CLASSES if c in classes(v)])
    # kill switch
    s = C.classify(PLAN, [rec(host="http://x:1")], expected_fn=stub_expected, env={"CONFORMANCE_CHECK": "0"})
    check("cls.kill_switch", s["skipped"] is True and s["ok"] is None and s["drift"] == [])
    check("cls.plan_object_accepted", C.classify(type("P", (), {"to_dict": lambda self: PLAN})(), [rec()], expected_fn=stub_expected)["ok"])
    check("cls.input_not_mutated", (lambda rs: (verdict(rs), rs)[1] == [rec(n=1)])([rec(n=1)]))


def t_real_profile():
    try:
        import model_profile as mp
    except ImportError:
        check("real.profile", True, "skipped")
        return
    err, sys.stderr = sys.stderr, open(os.devnull, "w")
    try:
        plan = {**PLAN, "model": MODEL}
        wire = mp.build_request_fields(MODEL, "author", "openai", overrides={"num_ctx": 65536}, lane="studio-db")
        rec_ok = rec(plan=plan, body={"model": MODEL, **wire}, role="author")
        v = C.classify(plan, [rec_ok])
        check("real.clean_against_yaml", v["ok"] is True, str(v))
        w2 = copy.deepcopy(wire); w2.pop("top_k")
        v = C.classify(plan, [rec(plan=plan, body={"model": MODEL, **w2}, role="author")])
        check("real.dropped_top_k_flagged", classes(v) == {"PARAM_DRIFT"}, str(v))
        w3 = mp.build_request_fields(MODEL, "author", "openai", lane="studio-db", sampling_step=1)
        v1 = C.classify(plan, [rec(plan=plan, body={"model": MODEL, **w3}, role="author", step=1)])
        check("real.ladder_step_consistent", v1["ok"] in (True, False))
        check("real.unknown_model_does_not_crash", C.classify({**plan, "model": "no-such-model"}, [rec(plan=plan)])["n"] == 1)
        sw = C.sweep([rec(plan=plan, body={"model": MODEL, **wire}, role="author"),
                      rec(plan=plan, body={"model": MODEL, **w2}, role="author", n=2)])
        check("real.sweep_flags_only_bad", len(sw) == 1 and sw[0]["class"] == "PARAM_DRIFT", str(sw))
    finally:
        sys.stderr.close()
        sys.stderr = err


def t_sweep_stub():
    good = rec(); bad = rec(n=2, body={"model": MODEL, **{**OPENAI_FIELDS, "top_k": 1}})
    sw = C.sweep([good, bad], expected_fn=stub_expected)
    check("sweep.flags_bad_only", len(sw) == 1 and sw[0]["model"] == MODEL)
    check("sweep.skips_compact_without_fields", C.sweep([C.build_record(now=1, model=MODEL, api="openai", body={"a": 1})], expected_fn=stub_expected) == [])


def t_import_safety():
    home = tmp()
    r = subprocess.run([sys.executable, "-c", f"import importlib.util as u;s=u.spec_from_file_location('c',{SRC!r});"
                        "m=u.module_from_spec(s);s.loader.exec_module(m)"],
                       env={"HOME": str(home), "PATH": os.environ.get("PATH", "")}, capture_output=True, text=True)
    check("import.clean", r.returncode == 0 and not r.stdout and not r.stderr and not any(home.iterdir()), r.stderr)
    shutil.rmtree(home, ignore_errors=True)


def t_mismatch_and_summary():
    d = tmp()
    SECRET = "SENTINEL-MISMATCH-KEY-zzzzzzzz"
    check("mm.writes", C.write_mismatch("HOST_DRIFT", "host", "http://a", "http://b", job="j1", att="a1", where="t", directory=d))
    check("mm.dedupes the same row", C.write_mismatch("HOST_DRIFT", "host", "http://a", "http://b", job="j1", att="a1", where="t", directory=d) is False)
    C.write_mismatch("KEY_DRIFT", "auth", SECRET, "x", job="j1", where="t", directory=d, secrets=[SECRET])
    rows = C.read_mismatches(d)
    check("mm.rows parsed, kind default mismatch", len(rows) == 2 and rows[0]["kind"] == "mismatch" and rows[0]["class"] == "HOST_DRIFT", rows)
    check("mm.secret scrubbed from the file", SECRET not in (d / C.MISMATCH_NAME).read_text())
    check("mm.mode 0600", oct((d / C.MISMATCH_NAME).stat().st_mode & 0o777) == "0o600")
    check("mm.CONFORMANCE_LOG=0 writes nothing", C.write_mismatch("HOST_DRIFT", "h2", "p", "l", directory=d, env={"CONFORMANCE_LOG": "0"}) is False)
    check("mm.since filter", C.read_mismatches(d, since=time.time() + 100) == [])
    with open(d / C.MISMATCH_NAME, "a") as fh:
        fh.write('{"torn": \n')
    check("mm.a torn line is skipped by the reader", len(C.read_mismatches(d)) == 2)
    d2 = tmp()
    _cap, C.MISMATCH_CAP_BYTES = C.MISMATCH_CAP_BYTES, 20000
    for i in range(400):
        C.write_mismatch("MODEL_DRIFT", "model", "p" * 200 + str(i), "l" * 200, job="j", directory=d2)
    big = [p for p in d2.glob("plan-mismatch.jsonl*")]
    C.MISMATCH_CAP_BYTES = _cap
    check("mm.size capped with one rotation", sum(p.stat().st_size for p in big) <= 2 * 20000 + 1024 and any(p.name.endswith(".1") for p in big), [(p.name, p.stat().st_size) for p in big])
    d3 = tmp()
    check("sum.writes", C.write_summary({"job": "j", "att": "a", "calls": 3, "recorded": 3}, directory=d3))
    check("sum.reads back", C.read_summaries(d3)[0]["calls"] == 3)
    check("sum.CONFORMANCE_LOG=0", C.write_summary({"job": "j"}, directory=d3, env={"CONFORMANCE_LOG": "0"}) is False)


def main():
    for t in (t_build_record, t_recorder_and_files, t_concurrent_append, t_prune_and_reader,
              t_classify_clean_and_each_drift, t_real_profile, t_sweep_stub, t_import_safety, t_mismatch_and_summary):
        try:
            t()
        except Exception as e:      # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAILS.append(f"{t.__name__}: {type(e).__name__}: {e}")
            print(f"FAIL {t.__name__} crashed: {e}")


MUTANTS = [
    ("if k in _DROP_KEYS or _SECRET_RE.search(str(k)):", "if k in _DROP_KEYS:"),
    ('_DROP_KEYS = {"messages", "prompt", "input", "tools", "functions", "system", "model", "stream_options"}',
     '_DROP_KEYS = {"prompt", "input", "tools", "functions", "system", "model", "stream_options"}'),
    ('out[k] = "present"', "out[k] = v"),
    ("return {k: _scrub_nested(x) for k, x in v.items() if not _SECRET_RE.search(str(k))}", "return v"),
    ("line, hit = _scrub_line(line, secrets)", "hit = False"),
    ('rec["kfp"] if False else None', "x") if False else ("\"kfp\": norm_fp(kfp) or key_fp8(key)", "\"kfp\": str(key)"),
    # flock removal is an EQUIVALENT mutant for line integrity (a single O_APPEND write is atomic); it only
    # guards the day-cap size check against a race, which is not deterministically testable.
    ("0o600)\n            try:\n                os.write", "0o644)\n            try:\n                os.write"),
    ("full = sig not in self._seen", "full = True"),
    ("full = sig not in self._seen", "full = not self._seen"),
    ("if size + len(line) + 1 > DAY_CAP_BYTES:", "if False:"),
    ("if full and len(_canon(rec.get(\"fields\", {}))) > MAX_FIELDS_BYTES:", "if False:"),
    ("        except Exception:        # noqa: BLE001 -- logging must never break a request\n            self.errors += 1\n            return False",
     "        except ZeroDivisionError:\n            self.errors += 1\n            return False"),
    ("if not log_enabled(self.env):\n                return False", "if False:\n                return False"),
    ("if p.stem < cutoff:", "if p.stem < cutoff and False:"),
    ("if p.stem < cutoff:", "if p.stem <= cutoff:"),
    ("        if total <= cap_bytes:\n            break", "        if True:\n            break"),
    ("                except ValueError:\n                    continue", "                except ValueError:\n                    raise"),
    ("                if not isinstance(r, dict):\n                    continue", "                pass"),
    ("if norm_url(r.get(\"host\")) != phost:", "if False:"),
    ("if r.get(\"api\") != papi or not any(path.startswith(x) for x in API_PATHS.get(papi, ())):", "if r.get(\"api\") != papi:"),
    ("if r.get(\"model\") != pmodel or (r.get(\"rmodel\") is not None and r.get(\"rmodel\") != pmodel):", "if r.get(\"model\") != pmodel:"),
    ("if r.get(\"model\") != pmodel or (r.get(\"rmodel\") is not None and r.get(\"rmodel\") != pmodel):", "if r.get(\"model\", \"\").lower() != pmodel.lower() or (r.get(\"rmodel\") is not None and r.get(\"rmodel\") != pmodel):"),
    ("if kfp is None or (kfp not in allowed_fps and not rotated_seen):", "if False:"),
    ("elif kfp is not None:", "elif False:"),
    ("if kfp is None or (kfp not in allowed_fps and not rotated_seen):", "if kfp is None:"),
    ("allowed_fps = {pfp} | {norm_fp(r.get(\"new_fp\")) for r in (pauth.get(\"rotations\") or [])}", "allowed_fps = {pfp}"),
    ("if p.get(\"num_ctx\") is not None and nc != p.get(\"num_ctx\"):", "if False:"),
    ("ign |= {\"options.num_ctx\"}           # judged as CTX_DRIFT", "pass"),
    ("for d in _subset_diff(exp, flds, ign):", "for d in _subset_diff(exp, flds, ign | set(exp)):"),
    ("ign = set(r.get(\"ov\") or ())\n            if papi", "ign = set()\n            if papi"),
    ("exp_cache[key] = exp_fn(p, role, step)", "exp_cache[key] = exp_fn(p, role, 0)"),
    ("if expected_calls is not None and expected_calls > n:", "if False:"),
    ("if any(r.get(\"_nofields\") for r in recs):", "if False:"),
    ("            if f is not None:\n                r[\"fields\"] = f", "            if False:\n                r[\"fields\"] = f"),
    ("elif isinstance(ev, dict) and isinstance(actual[k], dict):", "elif False:"),
    ("        if k not in actual:\n            diffs.append", "        if False:\n            diffs.append"),
    ("if not check_enabled(env):", "if False:"),
    ("drift = [found[c] for c in DRIFT_CLASSES if c in found]", "drift = list(found.values())[::-1]"),
    ("e[\"n\"] += 1", "e[\"n\"] = 1"),
    ("if (lo and p.stem < lo) or (hi and p.stem > hi):", "if False:"),
    ("if plan is not None and r.get(\"plan\") != plan:", "if False:"),
    ("if s and len(str(s)) >= 8 and str(s) in line:", "if False:"),
    ("if len(line) > MAX_RECORD_BYTES:\n            rec.pop(\"fields\", None)", "if False:\n            rec.pop(\"fields\", None)"),
]
MUTANTS += [
    ("if sig in _MISMATCH_SEEN:\n            return False", "if False:\n            return False"),
    ("if path.stat().st_size + len(line) + 1 > cap:", "if False:"),
    ("line = _scrub_line(_canon(rec), secrets)[0]\n    path = d / name", "line = _canon(rec)\n    path = d / name"),
    ("os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)\n        try:\n            os.write(fd, (line", "os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)\n        try:\n            os.write(fd, (line"),
    ("(r.get(\"t\") or 0) >= since", "True"),
]
MUTANTS = [m for m in MUTANTS if isinstance(m, tuple)]


def mutants():
    base = Path(SRC).read_text()
    bad = 0
    for i, (old, new) in enumerate(MUTANTS):
        if old not in base:
            print(f"MUTANT {i} NOT APPLICABLE: {old[:70]!r}")
            bad += 1
            continue
        d = tmp()
        (d / "conformance_log.py").write_text(base.replace(old, new, 1))
        try:
            r = subprocess.run([sys.executable, str(HERE / "test-conformance-log.py")],
                               env={**os.environ, "CONFORMANCE_LOG_SRC": str(d / "conformance_log.py")},
                               capture_output=True, text=True, timeout=120)
            killed = r.returncode != 0
        except subprocess.TimeoutExpired:
            killed = True
        print(f"mutant {i} ({old[:48]!r}): {'killed' if killed else 'SURVIVED'}")
        bad += not killed
        shutil.rmtree(d, ignore_errors=True)
    print("ALL MUTANTS KILLED" if not bad else f"{bad} MUTANT(S) SURVIVED / NOT APPLICABLE")
    return bad


if __name__ == "__main__":
    if "--mutants" in sys.argv or "--revert-check" in sys.argv:
        r = subprocess.run([sys.executable, str(Path(__file__).resolve())], capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stdout[-3000:])
            print("BASELINE FAILS: mutants meaningless")
            sys.exit(2)
        sys.exit(1 if mutants() else 0)
    main()
    print("ALL PASS" if not FAILS else f"FAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)
