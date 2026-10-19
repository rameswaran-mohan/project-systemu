"""R1 -- the Inbox card for a money / irreversible gate.

The card is the operator's only place to type the confirmation, so its pure model
must (a) carry the phrase, (b) never draw a standing option -- not even on a stale
card an older build posted with "Always allow" -- and (c) the click must resolve
through the SERVER-SIDE check (``inbox.resolve_pending_gate``), never straight
through ``queue.resolve``. NiceGUI-free, like the rest of the card's model tests.
"""
from __future__ import annotations

import pytest

from systemu.interface.command.gate import GateDescriptor


@pytest.fixture
def vault(tmp_path):
    from systemu.vault.vault import Vault
    root = tmp_path / "vault"
    for sub in ("scrolls", "activities", "shadow_army", "skills", "tools",
                "evolutions", "decisions"):
        (root / sub).mkdir(parents=True)
        (root / sub / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(root))


def _money_descriptor():
    return GateDescriptor.from_tool(
        tool_name="wire_funds", sig="s" * 40, verdict="require_approval",
        effect_tags=["money_move"], irreversible_effects=["money_move"],
        confirm_phrase="250")


def test_the_money_card_model_carries_the_phrase_and_no_standing_option():
    from systemu.interface.pages.inbox_page import _inbox_card_model
    model = _inbox_card_model(_money_descriptor())
    assert model["typed_confirm"] == "250"
    assert model["options"] == ["Deny", "Approve once"]
    assert model["affirmative"] == "Approve once"
    assert model["destructive"] is True
    assert model["risk"] == "high"


def test_a_stale_money_card_never_draws_always_allow():
    from systemu.interface.pages.inbox_page import _inbox_card_model
    ctx = {"kind": "gate", "gate_type": "tool", "risk": "medium",
           "tool_name": "wire_funds", "effect_tags": ["money_move"]}
    desc = GateDescriptor.from_decision_context(
        ctx, title="Run tool: wire_funds",
        options=["Deny", "Approve once", "Always allow"], dedup="tool:x")
    model = _inbox_card_model(desc)
    assert "Always allow" not in model["options"]
    assert model["typed_confirm"] == "wire_funds"
    assert model["risk"] == "high"


def test_an_ordinary_card_is_unchanged():
    from systemu.interface.pages.inbox_page import _inbox_card_model
    desc = GateDescriptor.from_tool(tool_name="sync_calendar", sig="c" * 40,
                                    verdict="require_approval",
                                    effect_tags=["net_mutate"])
    model = _inbox_card_model(desc)
    assert model["typed_confirm"] == ""
    assert model["options"] == ["Deny", "Approve once", "Always allow"]
    assert desc.risk == "medium"


def _post_money_card(vault):
    from systemu.approval.decision_queue import OperatorDecisionQueue
    desc = _money_descriptor()
    ctx = {**desc.to_decision_context(gate_type="tool"),
           "tool_name": "wire_funds", "tool_signature": "s" * 40,
           "verdict": "require_approval", "effect_tags": ["money_move"],
           "args_fingerprint": "a" * 40}
    return OperatorDecisionQueue(vault).post(
        title=desc.title, body=desc.inspect, options=desc.options, context=ctx,
        dedup_key=desc.dedup)


def test_the_card_click_resolves_through_the_server_side_check(vault):
    """The page's blocking half must go through ``resolve_pending_gate``: a click
    without the phrase records nothing; with it, the card resolves."""
    from systemu.interface.pages.inbox_page import (
        _resolve_and_execute_confirmed_gate, _resolve_and_execute_gate)
    dec_id = _post_money_card(vault)

    # the plain click path (a card that drew no box) goes through the check too
    res = _resolve_and_execute_gate(dec_id, "Approve once", vault)
    assert res.status.value == "noop"
    assert "type 250 exactly" in res.summary
    assert vault.get_decision(dec_id).status == "pending"

    res = _resolve_and_execute_confirmed_gate(dec_id, "Approve once", vault, "25")
    assert res.status.value == "noop"
    assert vault.get_decision(dec_id).status == "pending"

    res = _resolve_and_execute_confirmed_gate(dec_id, "Approve once", vault, "250")
    assert res.status.value == "ok", res.summary
    dec = vault.get_decision(dec_id)
    assert dec.status == "resolved" and dec.context["confirm_typed"] == "250"


def test_a_refused_click_reads_as_a_warning_toast(vault):
    from systemu.interface.pages.inbox_page import (
        _resolve_and_execute_confirmed_gate, gate_outcome_notice)
    dec_id = _post_money_card(vault)
    res = _resolve_and_execute_confirmed_gate(dec_id, "Approve once", vault, "")
    msg, kind = gate_outcome_notice(res, choice="Approve once")
    assert kind == "warning"
    assert "Not approved" in msg and "Nothing ran" in msg
    msg.encode("ascii")


def test_the_card_model_drops_a_standing_option_whatever_the_descriptor_says():
    """The renderer's own half: a descriptor that reached the page still listing
    "Always allow" next to an irreversible stamp (any producer, any build) draws
    no standing option."""
    from systemu.interface.pages.inbox_page import _inbox_card_model
    desc = GateDescriptor(title="Run tool: wire_funds", risk="medium",
                          options=["Deny", "Approve once", "Always allow"],
                          safe_default="Deny", dedup="tool:x",
                          irreversible_effects=["money_move"], confirm_phrase="250")
    model = _inbox_card_model(desc)
    assert model["options"] == ["Deny", "Approve once"]
    assert model["affirmative"] == "Approve once"
    assert model["risk"] == "high" and model["destructive"] is True
