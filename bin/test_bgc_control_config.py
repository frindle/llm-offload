import os

import bloomgauge_control as C


def test_defaults_and_paths(tmp_path):
    c = C.config({}, tmp_path)
    assert c["mode"] == "off" and c["home"] == str(tmp_path)
    assert c["lease_path"] == os.path.join(str(tmp_path), "bloomgauge-lease.json")
    assert c["shadow_lease_path"] == os.path.join(str(tmp_path), "bloomgauge-lease.shadow.json")
    assert c["ledger_path"] == os.path.join(str(tmp_path), "bloomgauge-control.jsonl")
    assert (c["mcp_path"], c["picks_per_h"], c["mgr_per_h"]) == ("", 2, 4)
    assert (c["idle_s"], c["quiet_s"], c["yield_s"], c["lease_ttl_s"], c["tick_s"]) == (900.0, 900.0, 7200.0, 600.0, 30.0)
    assert c["allow_picks"] is True and c["handback"] == "always" and c["heartbeat_at"] is None
    (tmp_path / "session-heartbeat").write_text("")
    os.utime(tmp_path / "session-heartbeat", (1234.0, 1234.0))
    assert C.config({}, tmp_path)["heartbeat_at"] == 1234.0


def test_mode_from_env_file_and_kill_switch(tmp_path):
    assert C.config({"QUEUE_BLOOMGAUGE_CONTROL": " Shadow "}, tmp_path)["mode"] == "shadow"
    assert C.config({"QUEUE_BLOOMGAUGE_CONTROL": "maybe"}, tmp_path)["mode"] == "off"
    (tmp_path / "bloomgauge-control.mode").write_text("ON\nset by owner\n")
    assert C.config({}, tmp_path)["mode"] == "on"
    assert C.config({"QUEUE_BLOOMGAUGE_CONTROL": "shadow"}, tmp_path)["mode"] == "shadow"
    (tmp_path / "bloomgauge-control.off").write_text("")
    assert C.config({"QUEUE_BLOOMGAUGE_CONTROL": "on"}, tmp_path)["mode"] == "off"


def test_empty_mode_file_is_off(tmp_path):
    (tmp_path / "bloomgauge-control.mode").write_text("\n")
    assert C.config({}, tmp_path)["mode"] == "off"
    (tmp_path / "bloomgauge-control.mode").write_text("bogus")
    assert C.config({}, tmp_path)["mode"] == "off"


def test_caps_numbers_flags_and_home_env(tmp_path):
    env = {"QUEUE_BG_PICKS_PER_H": "5", "QUEUE_BG_MANAGER_PER_H": "10", "QUEUE_BG_IDLE_MIN": "x", "QUEUE_BG_QUIET_MIN": "2",
           "QUEUE_BG_YIELD_S": "60", "QUEUE_BG_TICK_S": "5", "QUEUE_BG_LEASE_TTL_S": "90",
           "QUEUE_BG_ALLOW_PICKS": "0", "QUEUE_BG_HANDBACK": "prior", "QUEUE_BG_MCP_PATH": "/x/agent_mcp.py",
           "QUEUE_BG_HOME": str(tmp_path / "h")}
    c = C.config(env)
    assert (c["picks_per_h"], c["mgr_per_h"], c["idle_s"], c["quiet_s"], c["yield_s"]) == (2, 6, 900.0, 120.0, 60.0)
    assert (c["tick_s"], c["lease_ttl_s"], c["mcp_path"]) == (5.0, 90.0, "/x/agent_mcp.py")
    assert c["allow_picks"] is False and c["handback"] == "prior" and c["home"] == str(tmp_path / "h")
    low = C.config({"QUEUE_BG_PICKS_PER_H": "-1", "QUEUE_BG_MANAGER_PER_H": "1", "QUEUE_BG_ALLOW_PICKS": "yes",
                    "QUEUE_BG_HANDBACK": "never"}, tmp_path)
    assert (low["picks_per_h"], low["mgr_per_h"], low["allow_picks"], low["handback"]) == (0, 1, True, "always")


def test_unknown_env_mode_is_off_even_when_the_file_says_on(tmp_path):
    # 2026-11-03 review: an invalid, non-empty env value fell through to the mode file. The env wins when set.
    (tmp_path / "bloomgauge-control.mode").write_text("on\n")
    assert C.config({"QUEUE_BLOOMGAUGE_CONTROL": "maybe"}, tmp_path)["mode"] == "off"
    assert C.config({"QUEUE_BLOOMGAUGE_CONTROL": "  "}, tmp_path)["mode"] == "on"    # blank env: the file decides


def test_infinite_rate_caps_fall_back_to_their_defaults(tmp_path):
    # 2026-11-03 review: int(float("inf")) raised OverflowError out of config().
    c = C.config({"QUEUE_BG_PICKS_PER_H": "inf", "QUEUE_BG_MANAGER_PER_H": "-inf"}, tmp_path)
    assert (c["picks_per_h"], c["mgr_per_h"]) == (2, 4)
