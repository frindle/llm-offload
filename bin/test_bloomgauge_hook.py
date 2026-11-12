import importlib.util
import os
import re
from types import SimpleNamespace

_spec = importlib.util.spec_from_file_location(
    "oq_bloomgauge_hook", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ollama-queue.py"))
oq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oq)

PK = lambda j: None


def fake(wanted=True, raise_wanted=False, raise_run=False):
    calls = []

    def _wanted():
        if raise_wanted:
            raise RuntimeError("config broke")
        return wanted

    def _run(work, now):
        calls.append((work, now))
        if raise_run:
            raise RuntimeError("thread start failed")
        return True
    return SimpleNamespace(wanted=_wanted, run_async=_run), calls


def test_not_wanted_or_broken_does_nothing():
    bgt, calls = fake(wanted=False)
    state = {"jobs": [{"id": "aaaaaaaaaaaa1", "status": "running", "model": "m1"}]}
    assert oq.bloomgauge_hook(state, PK, 100.0, bgt=bgt) is None
    bgt2, calls2 = fake(raise_wanted=True)
    assert oq.bloomgauge_hook(state, PK, 100.0, bgt=bgt2) is None
    assert calls == [] and calls2 == []


def test_running_lane_work_is_busy_with_the_running_model():
    bgt, calls = fake()
    state = {"jobs": [{"id": "pppppppppppp1", "status": "pending", "model": "m-pending"},
                      {"id": "rrrrrrrrrrrr1", "status": "running", "model": "qwen3.6-35b"}]}
    work = oq.bloomgauge_hook(state, PK, 100.0, bgt=bgt)
    assert work["busy"] is True and work["target"] == "qwen3.6-35b"
    assert work["why"] == oq.bloom_queue_work_remaining(state, PK, 100.0)[1]
    assert calls == [(work, 100.0)]


def test_pending_only_and_ollama_tags(monkeypatch):
    monkeypatch.setattr(oq, "_darkbloom_url", lambda: None)      # hermetic: no live Darkbloom endpoint
    bgt, calls = fake()
    work = oq.bloomgauge_hook({"jobs": [{"id": "pppppppppppp2", "status": "pending", "model": "m-next"}]}, PK, 5.0, bgt=bgt)
    assert work["busy"] is True and work["target"] == "m-next"
    work = oq.bloomgauge_hook({"jobs": [{"id": "rrrrrrrrrrrr2", "status": "running", "model": "qwen3.8:27b"}]}, PK, 6.0, bgt=bgt)
    assert work["busy"] is True and work["target"] is None
    bgt3, calls3 = fake(raise_run=True)
    work = oq.bloomgauge_hook({"jobs": [{"id": "rrrrrrrrrrrr3", "status": "running", "model": "m1"}]}, PK, 7.0, bgt=bgt3)
    assert work["target"] == "m1" and len(calls3) == 1
    assert [c[1] for c in calls] == [5.0, 6.0]


def test_daemon_tick_calls_the_hook_after_the_plan_key_map():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ollama-queue.py")).read()
    m = re.search(r'\n(\s*)_pk = _plan_key_map\(state\["jobs"\]\)\n\s*_now = time\.time\(\)\n(\s*)(.*)\n', src)
    assert m, "daemon tick block not found"
    assert m.group(3).strip() == "bloomgauge_hook(state, _pk, _now)"
    assert m.group(2) == m.group(1)
    assert src.count("bloomgauge_hook(state, _pk, _now)") == 1


def test_an_ollama_tag_pins_the_darkbloom_model_it_runs_as(monkeypatch):
    monkeypatch.setattr(oq, "_darkbloom_url", lambda: "http://127.0.0.1:8000")
    monkeypatch.setattr(oq, "_darkbloom_model", lambda m: "qwen3.6-35b-a3b-vl-mtp-mxfp8" if m == "qwen3.8:27b-q4_K_M" else m)
    bgt, calls = fake()
    job = {"id": "rrrrrrrrrrrr4", "status": "running", "model": "qwen3.8:27b-q4_K_M"}
    assert oq.bloomgauge_hook({"jobs": [job]}, PK, 8.0, bgt=bgt)["target"] == "qwen3.6-35b-a3b-vl-mtp-mxfp8"
    job["model"] = "other:tag"                                       # maps to itself: still an Ollama tag
    assert oq.bloomgauge_hook({"jobs": [job]}, PK, 9.0, bgt=bgt)["target"] is None
    monkeypatch.setattr(oq, "_darkbloom_model", lambda m: (_ for _ in ()).throw(RuntimeError("profile broke")))
    assert oq.bloomgauge_hook({"jobs": [job]}, PK, 10.0, bgt=bgt)["target"] is None


def test_the_hook_raises_the_control_threads_alert_once(monkeypatch):
    seen = []
    monkeypatch.setattr(oq, "_control_alert", lambda key, msg: seen.append((key, msg)))
    bgt, calls = fake()
    bgt._STATE = {"alert": {"key": "bloomgauge-control-pick", "msg": "failed 3 picks"}}
    state = {"jobs": [{"id": "rrrrrrrrrrrr5", "status": "running", "model": "m1"}]}
    oq.bloomgauge_hook(state, PK, 11.0, bgt=bgt)
    oq.bloomgauge_hook(state, PK, 12.0, bgt=bgt)
    assert seen == [("bloomgauge-control-pick", "failed 3 picks")] and "alert" not in bgt._STATE
    bgt._STATE = {"alert": "not a dict"}
    oq.bloomgauge_hook(state, PK, 13.0, bgt=bgt)
    assert len(seen) == 1


def test_the_hook_hands_its_alert_writer_to_the_control_thread_before_running_it():
    seen = {}
    st = {}

    def _run(work, now):
        seen["deliver"] = st.get("deliver")
        return True
    bgt = SimpleNamespace(wanted=lambda: True, run_async=_run, _STATE=st)
    oq.bloomgauge_hook({"jobs": []}, PK, 9.0, bgt=bgt)
    assert seen["deliver"] is oq._control_alert
