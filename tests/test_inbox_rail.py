"""Import-light tests for the inbox_rail row-model helper (Task 16).

Mirrors the right_rail test style: assert the pure row-model maps a
GateDescriptor to a glance row carrying risk + the affirmative option label,
WITHOUT standing up a NiceGUI runtime.
"""
from __future__ import annotations

from systemu.interface.command.gate import GateDescriptor


def _rows(descriptors):
    from systemu.interface.components.inbox_rail import _inbox_rail_rows
    return _inbox_rail_rows(descriptors)


def _forge_descriptor():
    return GateDescriptor.from_forge({"id": "tool_x", "name": "fetch_json",
                                      "description": "Fetch JSON over HTTP"})


def test_row_carries_title_risk_and_affirmative_label():
    d = _forge_descriptor()
    rows = _rows([("dec_1", d)])
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "dec_1"
    assert row["title"] == d.title
    assert row["risk"] == "high"          # from_forge is high-risk
    # The affirmative option is the LAST option ("Forge"); the quick-approve
    # button is labeled with it so the operator knows what Approve does.
    assert row["approve_label"] == "Forge"
    assert d.options[-1] == "Forge"


def test_scroll_row_affirmative_is_approve():
    class _S:
        id = "scr_1"
        name = "morning_routine"
    d = GateDescriptor.from_scroll(_S(), summary="extract skills")
    rows = _rows([("dec_2", d)])
    assert rows[0]["risk"] == "medium"
    assert rows[0]["approve_label"] == "Approve"


def test_empty_descriptor_list_yields_no_rows():
    assert _rows([]) == []


def test_descriptor_with_no_options_has_empty_approve_label():
    d = GateDescriptor(title="bare", risk="low", options=[])
    rows = _rows([("dec_3", d)])
    assert rows[0]["approve_label"] == ""
    assert rows[0]["risk"] == "low"
