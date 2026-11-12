"""Tests for darkbloom_demand.py — shadow-mode, payout-aware model choice."""

import json
import os
import tempfile
import copy
import darkbloom_demand


def _make_fetcher(responses):
    """Return a fetch callable that returns pre-built JSON responses by URL path."""
    def fetch(url, timeout=20):
        path = url.replace(darkbloom_demand.BASE, "")
        if path in responses:
            return json.loads(json.dumps(responses[path]))
        raise Exception(f"Unexpected URL: {url}")
    return fetch


def _build_snapshot(models_dict=None, prices_dict=None, min_ram_dict=None, alias_of_dict=None):
    """Build a raw snapshot dict without calling load_snapshot."""
    return {
        "ts": 1000000,
        "models": models_dict or {},
        "prices": prices_dict or {},
        "min_ram_gb": min_ram_dict or {},
        "alias_of": alias_of_dict or {},
    }


# ---- TEST 1: fetch_json success ----
def test_fetch_json_success():
    called_urls = []
    def fake_opener(url, timeout):
        called_urls.append(url)
        return json.dumps({"ok": True}).encode()
    result = darkbloom_demand.fetch_json("http://test/api", opener=fake_opener)
    assert result == {"ok": True}
    assert len(called_urls) == 1
    assert called_urls[0] == "http://test/api"


# ---- TEST 2: fetch_json raises on error ----
def test_fetch_json_raises():
    def bad_opener(url, timeout):
        raise ConnectionError("network down")
    try:
        darkbloom_demand.fetch_json("http://test/x", opener=bad_opener)
        assert False, "Should have raised"
    except ConnectionError:
        pass


# ---- TEST 3: load_snapshot builds full structure ----
def test_load_snapshot_structure():
    responses = {
        "/api/network/models?window=24h": {
            "models": [
                {"model": "gemma-4-26b", "status": "needs_machines",
                 "demand": {"requests": 100, "served": 80,
                             "turned_away": {"no_free_machine": 10, "too_slow": 5, "timed_out": 5},
                             "input_tokens": 5e8, "output_tokens": 1e8, "published_hours": 24},
                 "supply": {"avg_loaded_machines": 368.6, "avg_busy_machines": 52.1, "busy_share": 0.141},
                 "per_machine_day": {"served_requests": 100, "input_tokens": 5e6, "output_tokens": 1e6}},
            ]
        },
        "/api/pricing": {
            "prices": [{"model": "gemma-4-26b-qat-4bit", "input_usd": "$0.0420", "output_usd": "$0.2200"}]
        },
        "/api/models": {
            "data": [{"id": "gemma-4-26b", "hugging_face_id": "gemma-4-26b-qat-4bit", "min_ram_gb": 36}],
            "aliases": [{"id": "gemma-4-26b", "primary_build": "gemma-4-26b-qat-4bit",
                         "desired_build": "gemma-4-26b-qat-8bit", "previous_build": "gemma-4-26b-8bit",
                         "retired_builds": ["gemma-4-26b-old"]}],
        },
    }
    fetch = _make_fetcher(responses)
    snap = darkbloom_demand.load_snapshot(fetch, now_ts=1000000)
    assert snap["ts"] == 1000000
    assert "gemma-4-26b" in snap["models"]
    assert snap["prices"]["gemma-4-26b-qat-4bit"]["in"] == 0.0420
    assert snap["prices"]["gemma-4-26b-qat-4bit"]["out"] == 0.2200
    assert snap["min_ram_gb"]["gemma-4-26b"] == 36.0
    assert snap["alias_of"]["gemma-4-26b-qat-4bit"] == "gemma-4-26b"
    assert snap["alias_of"]["gemma-4-26b-qat-8bit"] == "gemma-4-26b"
    assert snap["alias_of"]["gemma-4-26b-8bit"] == "gemma-4-26b"
    assert snap["alias_of"]["gemma-4-26b-old"] == "gemma-4-26b"


# ---- TEST 4: load_snapshot aliases from hugging_face_id too ----
def test_load_snapshot_hugging_face_alias():
    responses = {
        "/api/network/models?window=24h": {"models": []},
        "/api/pricing": {"prices": [{"model": "qwen-2-72b-qat", "input_usd": "$0.01", "output_usd": "$0.05"}]},
        "/api/models": {
            "data": [{"id": "qwen-2-72b", "hugging_face_id": "qwen-2-72b-qat", "min_ram_gb": 72}],
            "aliases": [],
        },
    }
    fetch = _make_fetcher(responses)
    snap = darkbloom_demand.load_snapshot(fetch, now_ts=2000000)
    assert snap["alias_of"]["qwen-2-72b-qat"] == "qwen-2-72b"


# ---- TEST 5: model_economics saturated model ----
def test_model_economics_saturated():
    row = {
        "status": "needs_machines",
        "demand": {"requests": 1000, "served": 800,
                    "turned_away": {"no_free_machine": 100, "too_slow": 50, "timed_out": 50},
                    "input_tokens": 5e9, "output_tokens": 1e9, "published_hours": 24},
        "supply": {"avg_loaded_machines": 100, "avg_busy_machines": 80, "busy_share": 0.8},
        "per_machine_day": {"served_requests": 100, "input_tokens": 5e7, "output_tokens": 1e7},
    }
    price = {"in": 0.042, "out": 0.22}
    eco = darkbloom_demand.model_economics(row, price)
    assert eco is not None
    assert eco["machines"] == 100
    assert eco["saturated"] is True
    # turned_away = 100+50+50 = 200; requests = 1000; share = 0.2
    assert eco["turned_away_share"] == 0.2
    # total_usd_day = (5e9*0.042 + 1e9*0.22)/1e6 * 24/24 = (210000000 + 220000000)/1e6 = 430
    assert abs(eco["total_usd_day"] - 430.0) < 0.01
    # per_machine_usd_day = (5e7*0.042 + 1e7*0.22)/1e6 = (2100000 + 2200000)/1e6 = 4.3
    assert abs(eco["per_machine_usd_day"] - 4.3) < 0.01
    # share_usd_day = 430/100 = 4.3
    assert abs(eco["share_usd_day"] - 4.3) < 0.01
    # marginal_new_usd_day = per_machine_usd_day (saturated) = 4.3
    assert abs(eco["marginal_new_usd_day"] - 4.3) < 0.01


# ---- TEST 6: model_economics unsaturated model ----
def test_model_economics_unsaturated():
    row = {
        "status": "balanced",
        "demand": {"requests": 500, "served": 480,
                    "turned_away": {"no_free_machine": 2, "too_slow": 2, "timed_out": 4},
                    "input_tokens": 2e9, "output_tokens": 5e8, "published_hours": 24},
        "supply": {"avg_loaded_machines": 200, "avg_busy_machines": 20, "busy_share": 0.1},
        "per_machine_day": {"served_requests": 50, "input_tokens": 2e7, "output_tokens": 5e6},
    }
    price = {"in": 0.042, "out": 0.22}
    eco = darkbloom_demand.model_economics(row, price)
    assert eco is not None
    # turned_away = 2+2+4 = 8; requests = 500; share = 8/500 = 0.016 <= 0.02 -> not saturated
    assert eco["turned_away_share"] == 0.016
    assert eco["saturated"] is False
    # total_usd_day = (2e9*0.042 + 5e8*0.22)/1e6 * 24/24 = (84000000 + 110000000)/1e6 = 194
    assert abs(eco["total_usd_day"] - 194.0) < 0.01
    # per_machine_usd_day = (2e7*0.042 + 5e6*0.22)/1e6 = (840000 + 1100000)/1e6 = 1.94
    assert abs(eco["per_machine_usd_day"] - 1.94) < 0.01
    # marginal_new_usd_day = total_usd_day/(machines+1) = 194/201
    assert abs(eco["marginal_new_usd_day"] - 194.0/201.0) < 0.01


# ---- TEST 7: model_economics missing data returns None ----
def test_model_economics_missing_data():
    row_no_supply = {"status": "balanced", "demand": {"requests": 100, "served": 90,
                                                        "turned_away": {}, "input_tokens": 0, "output_tokens": 0, "published_hours": 24},
                     "supply": None, "per_machine_day": {"served_requests": 0, "input_tokens": 0, "output_tokens": 0}}
    assert darkbloom_demand.model_economics(row_no_supply, {"in": 0.01, "out": 0.05}) is None

    row_no_pmd = {"status": "balanced", "demand": {"requests": 100, "served": 90,
                                                    "turned_away": {}, "input_tokens": 0, "output_tokens": 0, "published_hours": 24},
                  "supply": {"avg_loaded_machines": 10, "avg_busy_machines": 1, "busy_share": 0.1},
                  "per_machine_day": None}
    assert darkbloom_demand.model_economics(row_no_pmd, {"in": 0.01, "out": 0.05}) is None

    row_no_price = {"status": "balanced", "demand": {"requests": 100, "served": 90,
                                                      "turned_away": {}, "input_tokens": 0, "output_tokens": 0, "published_hours": 24},
                    "supply": {"avg_loaded_machines": 10, "avg_busy_machines": 1, "busy_share": 0.1},
                    "per_machine_day": {"served_requests": 10, "input_tokens": 1e6, "output_tokens": 1e6}}
    assert darkbloom_demand.model_economics(row_no_price, None) is None


# ---- TEST 8: update_history appends correctly and dedupes within 1800s ----
def test_update_history_dedupe_and_append():
    snap = _build_snapshot(
        models_dict={
            "gemma-4-26b": {"status": "needs_machines"},
            "qwen-2-72b": {"status": "balanced"},
        },
    )
    history = {}
    result = darkbloom_demand.update_history(history, snap, now_ts=1000000)
    assert len(result["gemma-4-26b"]) == 1
    assert result["gemma-4-26b"][0]["t"] == 1000000
    assert result["gemma-4-26b"][0]["status"] == "needs_machines"
    assert len(result["qwen-2-72b"]) == 1

    # Same ts -> within 1800s -> should NOT append again
    result2 = darkbloom_demand.update_history(result, snap, now_ts=1000000)
    assert len(result2["gemma-4-26b"]) == 1  # still just 1

    # 2000 seconds later -> should append
    result3 = darkbloom_demand.update_history(result2, snap, now_ts=1002000)
    assert len(result3["gemma-4-26b"]) == 2


# ---- TEST 9: update_history keep cap ----
def test_update_history_keep_cap():
    snap = _build_snapshot(
        models_dict={"mymodel": {"status": "balanced"}},
    )
    history = {}
    # Feed 60 entries at distinct hours (3600s apart)
    ts = 1000000
    for i in range(60):
        history = darkbloom_demand.update_history(history, snap, now_ts=ts)
        ts += 3600
    # Should be capped at 48
    assert len(history["mymodel"]) == 48
    # The oldest 12 should have been dropped; last entry ts should be 1000000 + 59*3600
    assert history["mymodel"][-1]["t"] == 1000000 + 59 * 3600
    # First entry ts should be 1000000 + 12*3600 (oldest kept)
    assert history["mymodel"][0]["t"] == 1000000 + 12 * 3600


# ---- TEST 10: update_history does not mutate input ----
def test_update_history_no_mutation():
    snap = _build_snapshot(
        models_dict={"mymodel": {"status": "balanced"}},
    )
    history = {"mymodel": [{"t": 100, "status": "old"}]}
    history_copy = copy.deepcopy(history)
    # Use timestamps far enough apart (2000s > 1800s)
    result = darkbloom_demand.update_history(history, snap, now_ts=2000)
    assert history == history_copy  # input unchanged
    assert len(result["mymodel"]) == 2  # but result has appended


# ---- TEST 11: persistently_needs ----
def test_persistently_needs():
    history = {
        "mymodel": [
            {"t": 100, "status": "needs_machines"},
            {"t": 200, "status": "needs_machines"},
            {"t": 300, "status": "needs_machines"},
            {"t": 400, "status": "needs_machines"},
        ],
    }
    assert darkbloom_demand.persistently_needs(history, "mymodel", n=3) is True  # last 3: needs, needs, needs
    assert darkbloom_demand.persistently_needs(history, "mymodel", n=4) is True  # all 4: needs
    assert darkbloom_demand.persistently_needs(history, "unknown_model", n=3) is False
    assert darkbloom_demand.persistently_needs(None, "mymodel", n=3) is False


# ---- TEST 12: decide — stay when queue_busy ----
def test_decide_stay_queue_busy():
    snap = _build_snapshot(
        models_dict={
            "gemma-4-26b": {
                "status": "balanced",
                "demand": {"requests": 500, "served": 480, "turned_away": {}, "input_tokens": 2e9, "output_tokens": 5e8, "published_hours": 24},
                "supply": {"avg_loaded_machines": 200, "avg_busy_machines": 20, "busy_share": 0.1},
                "per_machine_day": {"served_requests": 50, "input_tokens": 2e7, "output_tokens": 5e6},
            },
            "newmodel": {
                "status": "needs_machines",
                "demand": {"requests": 1000, "served": 800, "turned_away": {"no_free_machine": 100, "too_slow": 50, "timed_out": 50}, "input_tokens": 5e9, "output_tokens": 1e9, "published_hours": 24},
                "supply": {"avg_loaded_machines": 100, "avg_busy_machines": 80, "busy_share": 0.8},
                "per_machine_day": {"served_requests": 100, "input_tokens": 5e7, "output_tokens": 1e7},
            },
        },
        prices_dict={
            "current_build": {"in": 0.042, "out": 0.22},
            "new_build": {"in": 0.042, "out": 0.22},
        },
        alias_of_dict={
            "current_build": "gemma-4-26b",
            "new_build": "newmodel",
        },
        min_ram_dict={"gemma-4-26b": 36, "newmodel": 36},
    )
    decision = darkbloom_demand.decide(
        snap, history={}, current=["current_build"], allowed=["current_build", "new_build"],
        budget_gb=100, queue_busy=True, last_switch_ts=None, now_ts=1000000,
    )
    assert decision["action"] == "stay"
    assert decision["target"] is None
    assert any("queue" in r for r in decision["reasons"])


# ---- TEST 13: decide — stay inside cooldown ----
def test_decide_stay_cooldown():
    snap = _build_snapshot(
        models_dict={
            "gemma-4-26b": {
                "status": "balanced",
                "demand": {"requests": 500, "served": 480, "turned_away": {}, "input_tokens": 2e9, "output_tokens": 5e8, "published_hours": 24},
                "supply": {"avg_loaded_machines": 200, "avg_busy_machines": 20, "busy_share": 0.1},
                "per_machine_day": {"served_requests": 50, "input_tokens": 2e7, "output_tokens": 5e6},
            },
            "newmodel": {
                "status": "needs_machines",
                "demand": {"requests": 1000, "served": 800, "turned_away": {"no_free_machine": 100, "too_slow": 50, "timed_out": 50}, "input_tokens": 5e9, "output_tokens": 1e9, "published_hours": 24},
                "supply": {"avg_loaded_machines": 100, "avg_busy_machines": 80, "busy_share": 0.8},
                "per_machine_day": {"served_requests": 100, "input_tokens": 5e7, "output_tokens": 1e7},
            },
        },
        prices_dict={
            "current_build": {"in": 0.042, "out": 0.22},
            "new_build": {"in": 0.042, "out": 0.22},
        },
        alias_of_dict={
            "current_build": "gemma-4-26b",
            "new_build": "newmodel",
        },
        min_ram_dict={"gemma-4-26b": 36, "newmodel": 36},
    )
    # last_switch_ts = 960000, now_ts = 1000000 -> only 40000s elapsed, but cooldown is 43200
    decision = darkbloom_demand.decide(
        snap, history={}, current=["current_build"], allowed=["current_build", "new_build"],
        budget_gb=100, queue_busy=False, last_switch_ts=960000, now_ts=1000000,
    )
    # 1000000 - 960000 = 40000 < 43200 -> cooldown NOT elapsed
    assert decision["action"] == "stay"
    assert any("cooldown" in r for r in decision["reasons"])


# ---- TEST 14: decide — stay when gain below threshold ----
def test_decide_stay_gain_below_threshold():
    snap = _build_snapshot(
        models_dict={
            "gemma-4-26b": {
                "status": "balanced",
                "demand": {"requests": 500, "served": 480, "turned_away": {}, "input_tokens": 2e9, "output_tokens": 5e8, "published_hours": 24},
                "supply": {"avg_loaded_machines": 200, "avg_busy_machines": 20, "busy_share": 0.1},
                "per_machine_day": {"served_requests": 50, "input_tokens": 2e7, "output_tokens": 5e6},
            },
            "newmodel": {
                "status": "balanced",
                "demand": {"requests": 100, "served": 90, "turned_away": {}, "input_tokens": 1e8, "output_tokens": 2e7, "published_hours": 24},
                "supply": {"avg_loaded_machines": 200, "avg_busy_machines": 10, "busy_share": 0.05},
                "per_machine_day": {"served_requests": 10, "input_tokens": 1e6, "output_tokens": 2e5},
            },
        },
        prices_dict={
            "current_build": {"in": 0.042, "out": 0.22},
            "new_build": {"in": 0.042, "out": 0.22},
        },
        alias_of_dict={
            "current_build": "gemma-4-26b",
            "new_build": "newmodel",
        },
        min_ram_dict={"gemma-4-26b": 36, "newmodel": 36},
    )
    decision = darkbloom_demand.decide(
        snap, history={}, current=["current_build"], allowed=["current_build", "new_build"],
        budget_gb=100, queue_busy=False, last_switch_ts=None, now_ts=1000000,
    )
    assert decision["action"] == "stay"
    assert any("gain" in r for r in decision["reasons"])


# ---- TEST 15: decide — stay when model does not fit budget ----
def test_decide_stay_does_not_fit():
    snap = _build_snapshot(
        models_dict={
            "gemma-4-26b": {
                "status": "balanced",
                "demand": {"requests": 500, "served": 480, "turned_away": {}, "input_tokens": 2e9, "output_tokens": 5e8, "published_hours": 24},
                "supply": {"avg_loaded_machines": 200, "avg_busy_machines": 20, "busy_share": 0.1},
                "per_machine_day": {"served_requests": 50, "input_tokens": 2e7, "output_tokens": 5e6},
            },
            "huge_model": {
                "status": "needs_machines",
                "demand": {"requests": 1000, "served": 800, "turned_away": {"no_free_machine": 100, "too_slow": 50, "timed_out": 50}, "input_tokens": 5e9, "output_tokens": 1e9, "published_hours": 24},
                "supply": {"avg_loaded_machines": 100, "avg_busy_machines": 80, "busy_share": 0.8},
                "per_machine_day": {"served_requests": 100, "input_tokens": 5e7, "output_tokens": 1e7},
            },
        },
        prices_dict={
            "current_build": {"in": 0.042, "out": 0.22},
            "huge_build": {"in": 0.042, "out": 0.22},
        },
        alias_of_dict={
            "current_build": "gemma-4-26b",
            "huge_build": "huge_model",
        },
        min_ram_dict={"gemma-4-26b": 36, "huge_model": 200},  # huge_model needs 200GB
    )
    decision = darkbloom_demand.decide(
        snap, history={}, current=["current_build"], allowed=["current_build", "huge_build"],
        budget_gb=100, queue_busy=False, last_switch_ts=None, now_ts=1000000,
    )
    assert decision["action"] == "stay"
    assert decision["target"] is None
    assert any("fitting" in r for r in decision["reasons"])
    # Check candidate fits flag
    huge_cand = [c for c in decision["candidates"] if c["model"] == "huge_build"]
    assert len(huge_cand) == 1
    assert huge_cand[0]["fits"] is False


# ---- TEST 16: decide — saturated candidate without persistence stays ----
def test_decide_saturated_without_persistence():
    snap = _build_snapshot(
        models_dict={
            "gemma-4-26b": {
                "status": "balanced",
                "demand": {"requests": 500, "served": 480, "turned_away": {}, "input_tokens": 2e9, "output_tokens": 5e8, "published_hours": 24},
                "supply": {"avg_loaded_machines": 200, "avg_busy_machines": 20, "busy_share": 0.1},
                "per_machine_day": {"served_requests": 50, "input_tokens": 2e7, "output_tokens": 5e6},
            },
            "sat_model": {
                "status": "needs_machines",
                "demand": {"requests": 1000, "served": 800, "turned_away": {"no_free_machine": 100, "too_slow": 50, "timed_out": 50}, "input_tokens": 5e9, "output_tokens": 1e9, "published_hours": 24},
                "supply": {"avg_loaded_machines": 100, "avg_busy_machines": 80, "busy_share": 0.8},
                "per_machine_day": {"served_requests": 100, "input_tokens": 5e7, "output_tokens": 1e7},
            },
        },
        prices_dict={
            "current_build": {"in": 0.042, "out": 0.22},
            "sat_build": {"in": 0.042, "out": 0.22},
        },
        alias_of_dict={
            "current_build": "gemma-4-26b",
            "sat_build": "sat_model",
        },
        min_ram_dict={"gemma-4-26b": 36, "sat_model": 36},
    )
    history = {"sat_model": [
        {"t": 100, "status": "needs_machines"},
        {"t": 200, "status": "needs_machines"},
        {"t": 300, "status": "balanced"},  # breaks persistence
    ]}
    decision = darkbloom_demand.decide(
        snap, history=history, current=["current_build"], allowed=["current_build", "sat_build"],
        budget_gb=100, queue_busy=False, last_switch_ts=None, now_ts=1000000,
    )
    assert decision["action"] == "stay"
    assert any("persistence" in r for r in decision["reasons"])
    sat_cand = [c for c in decision["candidates"] if c["model"] == "sat_build"]
    assert len(sat_cand) == 1
    assert sat_cand[0]["persistent_need"] is False
    assert sat_cand[0]["saturated"] is True


# ---- TEST 17: decide — clear winner switches (unsaturated, high volume) ----
def test_decide_clear_winner_switches():
    snap = _build_snapshot(
        models_dict={
            "gemma-4-26b": {
                "status": "balanced",
                "demand": {"requests": 500, "served": 480, "turned_away": {}, "input_tokens": 2e9, "output_tokens": 5e8, "published_hours": 24},
                "supply": {"avg_loaded_machines": 200, "avg_busy_machines": 20, "busy_share": 0.1},
                "per_machine_day": {"served_requests": 50, "input_tokens": 2e7, "output_tokens": 5e6},
            },
            "win_model": {
                "status": "balanced",
                # turned_away = 2+2+4 = 8; requests = 5000; share = 8/5000 = 0.0016 <= 0.02 -> not saturated
                "demand": {"requests": 5000, "served": 4800, "turned_away": {"no_free_machine": 2, "too_slow": 2, "timed_out": 4}, "input_tokens": 2e10, "output_tokens": 5e9, "published_hours": 24},
                "supply": {"avg_loaded_machines": 200, "avg_busy_machines": 20, "busy_share": 0.1},
                "per_machine_day": {"served_requests": 500, "input_tokens": 2e8, "output_tokens": 5e7},
            },
        },
        prices_dict={
            "current_build": {"in": 0.042, "out": 0.22},
            "win_build": {"in": 0.042, "out": 0.22},
        },
        alias_of_dict={
            "current_build": "gemma-4-26b",
            "win_build": "win_model",
        },
        min_ram_dict={"gemma-4-26b": 36, "win_model": 36},
    )
    history = {"win_model": [
        {"t": 100, "status": "balanced"},
        {"t": 200, "status": "balanced"},
        {"t": 300, "status": "balanced"},
    ]}
    decision = darkbloom_demand.decide(
        snap, history=history, current=["current_build"], allowed=["current_build", "win_build"],
        budget_gb=100, queue_busy=False, last_switch_ts=None, now_ts=1000000,
    )
    assert decision["action"] == "switch"
    assert decision["target"] == "win_build"
    assert len(decision["reasons"]) == 0
    assert decision["gain_usd_day"] > 0


# ---- TEST 18: shadow_tick skips inside min_interval_s ----
def test_shadow_tick_skip_interval():
    tmpdir = tempfile.mkdtemp()
    state_path = os.path.join(tmpdir, "state.json")
    log_path = os.path.join(tmpdir, "log.jsonl")

    # Write initial state with last_run
    with open(state_path, "w") as f:
        json.dump({"last_run": 1000000, "history": {}, "last_switch_ts": None}, f)

    result = darkbloom_demand.shadow_tick(
        state_path, log_path, fetch=lambda p: {}, current=["a"], allowed=["a"],
        budget_gb=100, queue_busy=False, now_ts=1001000, min_interval_s=3600,
    )
    assert result is None  # skipped


# ---- TEST 19: shadow_tick writes one log line on success ----
def test_shadow_tick_writes_log():
    tmpdir = tempfile.mkdtemp()
    state_path = os.path.join(tmpdir, "state.json")
    log_path = os.path.join(tmpdir, "log.jsonl")

    # Raw API responses for load_snapshot
    responses = {
        "/api/network/models?window=24h": {
            "models": [
                {
                    "model": "gemma-4-26b",
                    "status": "balanced",
                    "demand": {"requests": 500, "served": 480, "turned_away": {}, "input_tokens": 2e9, "output_tokens": 5e8, "published_hours": 24},
                    "supply": {"avg_loaded_machines": 200, "avg_busy_machines": 20, "busy_share": 0.1},
                    "per_machine_day": {"served_requests": 50, "input_tokens": 2e7, "output_tokens": 5e6},
                },
            ]
        },
        "/api/pricing": {
            "prices": [{"model": "current_build", "input_usd": "$0.0420", "output_usd": "$0.2200"}]
        },
        "/api/models": {
            "data": [],
            "aliases": [],
        },
    }

    def my_fetch(path):
        return responses[path]

    result = darkbloom_demand.shadow_tick(
        state_path, log_path, fetch=my_fetch, current=["current_build"], allowed=["current_build"],
        budget_gb=100, queue_busy=False, now_ts=1000000, min_interval_s=0,
    )
    assert result is not None
    assert result["action"] == "stay"

    # Check log file has exactly 1 line
    with open(log_path, "r") as f:
        lines = f.readlines()
    assert len(lines) == 1

    entry = json.loads(lines[0])
    assert entry["ts"] == 1000000
    assert entry["action"] == "stay"


# ---- TEST 20: shadow_tick survives fetch exception ----
def test_shadow_tick_survives_exception():
    tmpdir = tempfile.mkdtemp()
    state_path = os.path.join(tmpdir, "state.json")
    log_path = os.path.join(tmpdir, "log.jsonl")

    # No state file -> empty state
    def bad_fetch(path):
        raise ConnectionError("down")

    result = darkbloom_demand.shadow_tick(
        state_path, log_path, fetch=bad_fetch, current=["a"], allowed=["a"],
        budget_gb=100, queue_busy=False, now_ts=1000000, min_interval_s=0,
    )
    assert result["action"] == "stay"
    assert "error" in result

    # Check log has error line
    with open(log_path, "r") as f:
        lines = f.readlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert "error" in entry


# ---- TEST 21: shadow_tick survives corrupt state file ----
def test_shadow_tick_survives_corrupt_state():
    tmpdir = tempfile.mkdtemp()
    state_path = os.path.join(tmpdir, "state.json")
    log_path = os.path.join(tmpdir, "log.jsonl")

    # Write corrupt state
    with open(state_path, "w") as f:
        f.write("not valid json {{{")

    responses = {
        "/api/network/models?window=24h": {
            "models": [
                {
                    "model": "gemma-4-26b",
                    "status": "balanced",
                    "demand": {"requests": 500, "served": 480, "turned_away": {}, "input_tokens": 2e9, "output_tokens": 5e8, "published_hours": 24},
                    "supply": {"avg_loaded_machines": 200, "avg_busy_machines": 20, "busy_share": 0.1},
                    "per_machine_day": {"served_requests": 50, "input_tokens": 2e7, "output_tokens": 5e6},
                },
            ]
        },
        "/api/pricing": {"prices": [{"model": "current_build", "input_usd": "$0.0420", "output_usd": "$0.2200"}]},
        "/api/models": {"data": [], "aliases": []},
    }

    result = darkbloom_demand.shadow_tick(
        state_path, log_path, fetch=lambda p: responses[p], current=["current_build"], allowed=["current_build"],
        budget_gb=100, queue_busy=False, now_ts=1000000, min_interval_s=0,
    )
    assert result is not None
    assert result["action"] == "stay"


# ---- TEST 22: shadow_tick sets last_run but not last_switch_ts ----
def test_shadow_tick_updates_state():
    tmpdir = tempfile.mkdtemp()
    state_path = os.path.join(tmpdir, "state.json")
    log_path = os.path.join(tmpdir, "log.jsonl")

    responses = {
        "/api/network/models?window=24h": {
            "models": [
                {
                    "model": "gemma-4-26b",
                    "status": "balanced",
                    "demand": {"requests": 500, "served": 480, "turned_away": {}, "input_tokens": 2e9, "output_tokens": 5e8, "published_hours": 24},
                    "supply": {"avg_loaded_machines": 200, "avg_busy_machines": 20, "busy_share": 0.1},
                    "per_machine_day": {"served_requests": 50, "input_tokens": 2e7, "output_tokens": 5e6},
                },
            ]
        },
        "/api/pricing": {"prices": [{"model": "current_build", "input_usd": "$0.0420", "output_usd": "$0.2200"}]},
        "/api/models": {"data": [], "aliases": []},
    }

    with open(state_path, "w") as f:
        json.dump({"last_run": 0, "history": {}, "last_switch_ts": 500000}, f)

    result = darkbloom_demand.shadow_tick(
        state_path, log_path, fetch=lambda p: responses[p], current=["current_build"], allowed=["current_build"],
        budget_gb=100, queue_busy=False, now_ts=1000000, min_interval_s=0,
    )

    # Read back state
    with open(state_path, "r") as f:
        saved = json.load(f)
    assert saved["last_run"] == 1000000
    # last_switch_ts should NOT be updated in shadow mode
    assert saved.get("last_switch_ts") is None or saved.get("last_switch_ts") == 500000


# ---- TEST 23: model_economics published_hours defaults to 24 ----
def test_model_economics_published_hours_default():
    row = {
        "status": "balanced",
        "demand": {"requests": 500, "served": 480, "turned_away": {}, "input_tokens": 2e9, "output_tokens": 5e8, "published_hours": 0},
        "supply": {"avg_loaded_machines": 200, "avg_busy_machines": 20, "busy_share": 0.1},
        "per_machine_day": {"served_requests": 50, "input_tokens": 2e7, "output_tokens": 5e6},
    }
    eco = darkbloom_demand.model_economics(row, {"in": 0.042, "out": 0.22})
    assert eco is not None
    # With published_hours=0, it defaults to 24
    expected_total = (2e9 * 0.042 + 5e8 * 0.22) / 1e6 * 24 / 24
    assert abs(eco["total_usd_day"] - expected_total) < 0.01


# ---- TEST 24: decide no fitting candidate when all excluded ----
def test_decide_no_fitting_candidate_all_current():
    snap = _build_snapshot(
        models_dict={
            "gemma-4-26b": {
                "status": "balanced",
                "demand": {"requests": 500, "served": 480, "turned_away": {}, "input_tokens": 2e9, "output_tokens": 5e8, "published_hours": 24},
                "supply": {"avg_loaded_machines": 200, "avg_busy_machines": 20, "busy_share": 0.1},
                "per_machine_day": {"served_requests": 50, "input_tokens": 2e7, "output_tokens": 5e6},
            },
        },
        prices_dict={"current_build": {"in": 0.042, "out": 0.22}},
        alias_of_dict={"current_build": "gemma-4-26b"},
        min_ram_dict={"gemma-4-26b": 36},
    )
    decision = darkbloom_demand.decide(
        snap, history={}, current=["current_build"], allowed=["current_build"],
        budget_gb=100, queue_busy=False, last_switch_ts=None, now_ts=1000000,
    )
    assert decision["action"] == "stay"
    assert decision["target"] is None
    assert len(decision["candidates"]) == 0  # no candidates because all are current


# Run all tests

# ---- reviewer regression tests ----
def test_shadow_tick_error_preserves_history():
    tmpdir = tempfile.mkdtemp()
    state_path = os.path.join(tmpdir, "state.json")
    log_path = os.path.join(tmpdir, "log.jsonl")
    hist = {"m": [{"t": 1, "status": "needs_machines"}]}
    with open(state_path, "w") as f:
        json.dump({"last_run": 0, "history": hist, "last_switch_ts": 5}, f)
    def bad_fetch(path):
        raise ConnectionError("down")
    r = darkbloom_demand.shadow_tick(state_path, log_path, fetch=bad_fetch, current=["a"], allowed=["a"],
                                     budget_gb=100, queue_busy=False, now_ts=1000000, min_interval_s=0)
    assert r["action"] == "stay"
    with open(state_path) as f:
        st = json.load(f)
    assert st == {"last_run": 1000000, "history": hist, "last_switch_ts": 5}


def test_shadow_tick_non_dict_state_never_raises():
    tmpdir = tempfile.mkdtemp()
    state_path = os.path.join(tmpdir, "state.json")
    log_path = os.path.join(tmpdir, "log.jsonl")
    with open(state_path, "w") as f:
        f.write("[1, 2, 3]")
    def bad_fetch(path):
        raise ConnectionError("down")
    r = darkbloom_demand.shadow_tick(state_path, log_path, fetch=bad_fetch, current=["a"], allowed=["a"],
                                     budget_gb=100, queue_busy=False, now_ts=1000000, min_interval_s=0)
    assert r["action"] == "stay"
    assert r["error"] == "ConnectionError: down"


def test_update_history_keeps_absent_models():
    snap = {"ts": 10000, "models": {"b": {"model": "b", "status": "balanced"}}, "prices": {}, "min_ram_gb": {}, "alias_of": {}}
    h = darkbloom_demand.update_history({"a": [{"t": 1, "status": "crowded"}]}, snap, 10000)
    assert h == {"a": [{"t": 1, "status": "crowded"}], "b": [{"t": 10000, "status": "balanced"}]}

if __name__ == "__main__":
    tests = [
        test_fetch_json_success,
        test_fetch_json_raises,
        test_load_snapshot_structure,
        test_load_snapshot_hugging_face_alias,
        test_model_economics_saturated,
        test_model_economics_unsaturated,
        test_model_economics_missing_data,
        test_update_history_dedupe_and_append,
        test_update_history_keep_cap,
        test_update_history_no_mutation,
        test_persistently_needs,
        test_decide_stay_queue_busy,
        test_decide_stay_cooldown,
        test_decide_stay_gain_below_threshold,
        test_decide_stay_does_not_fit,
        test_decide_saturated_without_persistence,
        test_decide_clear_winner_switches,
        test_shadow_tick_skip_interval,
        test_shadow_tick_writes_log,
        test_shadow_tick_survives_exception,
        test_shadow_tick_survives_corrupt_state,
        test_shadow_tick_updates_state,
        test_model_economics_published_hours_default,
        test_decide_no_fitting_candidate_all_current,
        test_shadow_tick_error_preserves_history,
        test_shadow_tick_non_dict_state_never_raises,
        test_update_history_keeps_absent_models,
    ]

    failed = 0
    passed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS: {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {t.__name__}: {e}")
            failed += 1

    print(f"\n{passed}/{passed+failed} passed")
    if failed > 0:
        import sys
        sys.exit(1)
    else:
        print("ALL PASS")
