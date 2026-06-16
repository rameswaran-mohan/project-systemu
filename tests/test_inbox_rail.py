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


# ── v0.9.32 (D.4 review FIX-3): command gates are render-only in the rail ──────
def _command_descriptor():
    return GateDescriptor.from_command(
        tool_name="run_command", command="rm -rf build", cwd="/proj")


def test_command_gate_row_is_render_only_no_quick_approve():
    """A command gate's affirmative option is 'Always allow' — letting one rail
    click pick it (with no Deny) would be dangerous AND resolve_gate NOOPs for it
    so it would not even persist. The rail row must be render-only: NO
    approve_label (no one-click quick-approve button)."""
    d = _command_descriptor()
    # The descriptor's affirmative IS the dangerous 'Always allow'...
    assert d.options[-1] == "Always allow"
    rows = _rows([("dec_cmd", d)])
    row = rows[0]
    # ...but the rail must NOT surface it as a one-click approve.
    assert row["render_only"] is True
    assert row["approve_label"] == ""
    # The card itself still renders (title/risk preserved).
    assert row["title"] == d.title
    assert row["risk"] == "high"


def test_non_command_gate_still_gets_quick_approve():
    """Regression: render-only is scoped to command gates only — a forge gate
    still surfaces its affirmative quick-approve label."""
    rows = _rows([("dec_f", _forge_descriptor())])
    assert rows[0]["render_only"] is False
    assert rows[0]["approve_label"] == "Forge"


def test_approve_descriptor_refuses_command_gate():
    """Defense-in-depth: even if invoked directly, the one-click rail approver
    REFUSES a command gate (it must go through the /insights three-way UI). It
    must NOT NOOP-resolve it (the old behavior, which left the gate unresolved
    AND unpersisted)."""
    import pytest
    from systemu.interface.components.inbox_rail import _approve_descriptor

    resolved = {"called": False}

    class _Queue:
        def resolve(self, *a, **k):
            resolved["called"] = True

    class _Vault:
        pass

    d = _command_descriptor()
    with pytest.raises(ValueError):
        _approve_descriptor("dec_cmd", d, vault=_Vault())
    # It refused BEFORE resolving — no mis-resolve, no NOOP.
    assert resolved["called"] is False
