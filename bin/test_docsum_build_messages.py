import pytest
import docsum_core as dc

SC = "I am a tenant deciding whether to renew"
TX = "[Page 3]\nRent is $1,250.00 due 2026-12-01\n"


def test_system_rules_present_for_every_kind():
    for kind in ("single", "map", "reduce"):
        system, user = dc.build_messages(SC, TX, kind)
        low = system.lower()
        assert "never invent" in low
        assert "exact" in low and "page" in low
        assert isinstance(user, str)


def test_user_message_carries_scenario_and_text():
    for kind in ("single", "map", "reduce"):
        _, user = dc.build_messages(SC, TX, kind)
        assert SC in user
        assert "Rent is $1,250.00 due 2026-12-01" in user


def test_single_and_reduce_request_all_sections_map_does_not():
    for kind in ("single", "reduce"):
        _, user = dc.build_messages(SC, TX, kind)
        for name in dc.SECTIONS:
            assert "## " + name in user
    _, mapu = dc.build_messages(SC, TX, "map")
    assert "## Bottom line" not in mapu
    assert "page numbers" in mapu.lower()


def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        dc.build_messages(SC, TX, "bogus")


def test_section_headings_are_one_per_line_in_order():
    for kind in ("single", "reduce"):
        _, user = dc.build_messages(SC, TX, kind)
        lines = user.splitlines()
        heads = [ln for ln in lines if ln.startswith("## ")]
        assert heads == ["## " + s for s in dc.SECTIONS]
