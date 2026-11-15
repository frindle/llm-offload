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


# ---- the queue runs away from the Mac (QUEUE_AGENT_URL): the Mac status service runs the step (2026-11-12)
def _remote_reset(monkeypatch):
    monkeypatch.setattr(oq, "_BG_REMOTE", {"thread": None, "last": None, "ack": 0, "epoch": None, "fails": 0})
    monkeypatch.setattr(oq, "_BG_MODE", {"at": None, "mode": "off"})
    monkeypatch.setattr(oq.queue_config, "agent_url", lambda: "http://mac-agent.invalid:7691")


def test_remote_hook_posts_the_work_from_a_thread_and_never_imports_the_tick(monkeypatch):
    _remote_reset(monkeypatch)
    import sys as _sys
    monkeypatch.delitem(_sys.modules, "bloomgauge_tick", raising=False)
    posted = []
    monkeypatch.setattr(oq.queue_config, "agent_post",
                        lambda path, body, timeout=5.0: posted.append((path, body, timeout)) or
                        {"epoch": "e1", "mode": "on", "alerts": []})
    state = {"jobs": [{"id": "rrrrrrrrrrrr9", "status": "running", "model": "m1"}]}
    work = oq.bloomgauge_hook(state, PK, 100.0)
    assert work == {"busy": True, "why": oq.bloom_queue_work_remaining(state, PK, 100.0)[1], "target": "m1"}
    oq._BG_REMOTE["thread"].join(5)
    assert posted == [("/v1/bloomgauge-tick", dict(work, ack=0, epoch=None), oq.BG_REMOTE_TIMEOUT_S)]
    assert "bloomgauge_tick" not in _sys.modules
    assert oq._BG_MODE["mode"] == "on" and oq._BG_REMOTE["epoch"] == "e1"
    assert oq.bloomgauge_hook(state, PK, 105.0) is None                     # within BG_REMOTE_EVERY_S
    assert oq.bloomgauge_hook(state, PK, 100.0 + oq.BG_REMOTE_EVERY_S) is not None
    oq._BG_REMOTE["thread"].join(5)
    assert len(posted) == 2 and posted[1][1]["epoch"] == "e1"


def test_remote_hook_skips_while_an_exchange_is_in_flight(monkeypatch):
    import threading
    _remote_reset(monkeypatch)
    gate, calls = threading.Event(), []

    def slow(path, body, timeout=5.0):
        calls.append(body)
        gate.wait(5)
        return None
    monkeypatch.setattr(oq.queue_config, "agent_post", slow)
    assert oq.bloomgauge_hook({"jobs": []}, PK, 10.0) is not None
    assert oq.bloomgauge_hook({"jobs": []}, PK, 100.0) is None              # previous still waiting
    gate.set()
    oq._BG_REMOTE["thread"].join(5)
    assert len(calls) == 1 and oq._BG_REMOTE["fails"] == 1


def test_remote_round_raises_each_alert_once_per_epoch(monkeypatch):
    _remote_reset(monkeypatch)
    seen = []
    monkeypatch.setattr(oq, "_control_alert", lambda key, msg: seen.append((key, msg)))
    answer = {"epoch": "e1", "mode": "shadow", "alerts": [
        {"seq": 2, "key": "bloomgauge-control-manager", "msg": "3 Manager writes failed"},
        {"seq": 1, "key": "bloomgauge-control-pick", "msg": "3 picks failed"},
        {"seq": True, "key": "x", "msg": "bool seq ignored"}, "junk"]}
    sent = []
    post = lambda path, body, timeout=5.0: sent.append(dict(body)) or answer
    W = {"busy": False, "why": "queue done", "target": None}
    assert oq._bloomgauge_remote_round(W, post) == 2
    assert seen == [("bloomgauge-control-pick", "3 picks failed"),
                    ("bloomgauge-control-manager", "3 Manager writes failed")]
    assert oq._BG_REMOTE["ack"] == 2 and oq._BG_MODE["mode"] == "shadow"
    assert oq._bloomgauge_remote_round(W, post) == 0 and len(seen) == 2      # the service resent them: acked
    assert sent[1]["ack"] == 2 and sent[1]["epoch"] == "e1"
    answer.update(epoch="e2", alerts=[{"seq": 1, "key": "bloomgauge-control-log", "msg": "log unreadable"}])
    assert oq._bloomgauge_remote_round(W, post) == 1                        # restarted service: numbers anew
    assert seen[-1] == ("bloomgauge-control-log", "log unreadable") and oq._BG_REMOTE["ack"] == 1


def test_remote_round_without_an_answer_raises_nothing(monkeypatch):
    _remote_reset(monkeypatch)
    seen = []
    monkeypatch.setattr(oq, "_control_alert", lambda key, msg: seen.append((key, msg)))
    W = {"busy": True, "why": "x", "target": None}
    assert oq._bloomgauge_remote_round(W, lambda *a, **k: None) is None
    assert oq._bloomgauge_remote_round(W, lambda *a, **k: (_ for _ in ()).throw(OSError("down"))) is None
    assert oq._BG_REMOTE["fails"] == 2 and seen == [] and oq._BG_MODE["at"] is None


def test_control_mode_comes_from_the_mac_when_remote(monkeypatch):
    _remote_reset(monkeypatch)
    got = []
    monkeypatch.setattr(oq.queue_config, "agent_get", lambda path, timeout=3.0, text=False:
                        got.append(path) or {"mode": "shadow"})
    assert oq._bloomgauge_control_mode(1000.0) == "shadow" and got == ["/v1/bloomgauge-tick"]
    assert oq._bloomgauge_control_mode(1001.0) == "shadow" and len(got) == 1     # cached
    monkeypatch.setattr(oq, "_BG_MODE", {"at": None, "mode": "off"})
    monkeypatch.setattr(oq.queue_config, "agent_get", lambda *a, **k: None)
    assert oq._bloomgauge_control_mode(2000.0) == "off"


def test_local_hook_unchanged_when_the_agent_url_is_unset(monkeypatch):
    import sys as _sys
    monkeypatch.setattr(oq.queue_config, "agent_url", lambda: None)
    monkeypatch.setattr(oq.queue_config, "agent_post",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no remote call")))
    bgt, calls = fake()
    monkeypatch.setitem(_sys.modules, "bloomgauge_tick", bgt)       # what `import bloomgauge_tick` finds
    work = oq.bloomgauge_hook({"jobs": [{"id": "rrrrrrrrrrrr8", "status": "running", "model": "m1"}]}, PK, 50.0)
    assert work["target"] == "m1" and calls == [(work, 50.0)]
