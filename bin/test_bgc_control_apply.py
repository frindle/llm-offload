import json

import bloomgauge_control as C


class Unavailable(Exception):
    pass


class Client:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def set_manager(self, args):
        self.calls.append(("set_manager", args))
        if self.fail:
            raise Unavailable(self.fail)

    def pick_model(self, args):
        self.calls.append(("pick_model", args))


def rows(p):
    return [json.loads(x) for x in p.read_text().splitlines()]


def test_shadow_logs_would_do_and_sends_nothing(tmp_path):
    p, cl = tmp_path / "l.jsonl", Client()
    r = C.apply({"action": "mgr_off", "model": "m1", "reason": "acquire"}, cl, "shadow", str(p), 10.0)
    assert r == {"ok": True, "posted": False, "err": None, "op_id": None}
    assert cl.calls == []
    assert rows(p) == [{"ts": 10.0, "action": "mgr_off", "model": "m1", "reason": "acquire", "mode": "shadow", "phase": "shadow"}]


def test_on_logs_attempt_then_result_and_calls_client(tmp_path):
    p, cl = tmp_path / "l.jsonl", Client()
    r = C.apply({"action": "mgr_on", "model": None, "reason": "handback", "cause": "deadman"}, cl, "on", str(p), 20.0)
    assert r == {"ok": True, "posted": True, "err": None, "op_id": None}   # the answer named no operation
    assert cl.calls == [("set_manager", {"on": True})]
    got = rows(p)
    assert [(x["phase"], x.get("result"), x["cause"]) for x in got] == [("attempt", None, "deadman"), ("result", "ok", "deadman")]
    C.apply({"action": "pick", "model": "m2", "reason": "hold"}, cl, "on", str(p), 30.0)
    C.apply({"action": "mgr_off", "model": None, "reason": "acquire"}, cl, "on", str(p), 31.0)
    assert cl.calls[1:] == [("pick_model", {"model": "m2"}), ("set_manager", {"on": False})]
    assert "cause" not in rows(p)[-1]


def test_on_failure_is_logged_and_reported(tmp_path):
    p = tmp_path / "l.jsonl"
    r = C.apply({"action": "mgr_on", "model": None, "reason": "handback"}, Client(fail="TURN_ON " + "x" * 200), "on", str(p), 40.0)
    assert r["ok"] is False and r["posted"] is True and r["op_id"] is None
    assert r["err"].startswith("Unavailable: TURN_ON") and len(r["err"]) == 120
    last = rows(p)[-1]
    assert (last["phase"], last["result"], last["err"]) == ("result", "failed", r["err"])
    assert rows(p)[0]["phase"] == "attempt"


def test_non_write_decisions_are_logged_once_until_they_change(tmp_path):
    p = tmp_path / "l.jsonl"
    d = {"action": "wait", "model": None, "reason": "handback blocked: wider-list"}
    assert C.apply(d, None, "shadow", str(p), 1.0) == {"ok": True, "posted": False, "err": None, "op_id": None}
    C.apply(d, None, "shadow", str(p), 2.0)
    assert [x["ts"] for x in rows(p)] == [1.0]
    assert rows(p)[0]["phase"] == "decision"
    C.apply(dict(d, reason="drain: within grace"), None, "shadow", str(p), 3.0)
    C.apply({"action": "mgr_on", "model": None, "reason": "x"}, None, "shadow", str(p), 4.0)
    C.apply(d, None, "shadow", str(p), 5.0)
    assert [(x["ts"], x["phase"]) for x in rows(p)] == [(1.0, "decision"), (3.0, "decision"), (4.0, "shadow"), (5.0, "decision")]


class Answering(Client):
    """set_manager answers like BloomGauge's set-automatic: its snapshot (operation, lastRequestId)."""
    def __init__(self, answer):
        super().__init__()
        self.answer = answer

    def set_manager(self, args):
        self.calls.append(("set_manager", args))
        return self.answer


def test_mgr_on_keeps_the_operation_id_of_the_answer(tmp_path):
    p = tmp_path / "l.jsonl"
    ans = {"operation": {"id": "op-1", "status": "pending", "detail": "Starting"}, "lastRequestId": "op-1"}
    r = C.apply({"action": "mgr_on", "model": None, "reason": "handback"}, Answering(ans), "on", str(p), 50.0)
    assert r == {"ok": True, "posted": True, "err": None, "op_id": "op-1"}
    got = rows(p)
    assert [x["phase"] for x in got] == ["attempt", "result"]
    assert "op_id" not in got[0] and got[1]["op_id"] == "op-1" and got[1]["result"] == "ok"
    # a Manager-off answer is not followed: no op_id in its row
    r = C.apply({"action": "mgr_off", "model": None, "reason": "acquire"}, Answering(ans), "on", str(p), 51.0)
    assert r["op_id"] is None and "op_id" not in rows(p)[-1]


def test_op_id_of_reads_only_a_consistent_answer():
    assert C.op_id_of({"operation": {"id": "a"}, "lastRequestId": "a"}) == "a"
    assert C.op_id_of({"operation": {"id": "a"}}) == "a"                       # no lastRequestId: the operation
    assert C.op_id_of({"operation": {"id": "a"}, "lastRequestId": "b"}) is None  # a newer request: not ours
    for bad in (None, "x", [], {}, {"operation": None}, {"operation": {"id": ""}}, {"operation": {"id": 7}},
                {"operation": "a"}, {"lastRequestId": "a"}):
        assert C.op_id_of(bad) is None, bad
    # a mgr_on answered without an operation is logged with op_id None (the tick then releases its lease)
    import tempfile, os
    with tempfile.TemporaryDirectory() as d:
        lp = os.path.join(d, "l.jsonl")
        r = C.apply({"action": "mgr_on", "model": None, "reason": "x"}, Answering({"ok": True}), "on", lp, 1.0)
        assert r["op_id"] is None and json.loads(open(lp).read().splitlines()[-1])["op_id"] is None


def test_record_outcome_appends_one_outcome_row(tmp_path):
    p = tmp_path / "l.jsonl"
    row = C.record_outcome(str(p), 50.0, "op-1", "blocked", "Could not verify\n this   command." + "x" * 300, 99.0, "on")
    assert rows(p) == [row]
    assert row["phase"] == "outcome" and row["action"] == "mgr_on" and row["mode"] == "on"
    assert (row["ts"], row["attempt_ts"], row["op_id"], row["status"]) == (99.0, 50.0, "op-1", "blocked")
    assert row["detail"].startswith("Could not verify this command.") and len(row["detail"]) == 200
    assert row["reason"] == "turn-on blocked"
