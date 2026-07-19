"""EMPTY / UNKNOWN effect_tags must FLOOR remote (phone) approval.

An empty ``effect_tags`` list is the ABSENCE of a classification, not a finding
of "no effect". ``classify_resolution`` read it as the latter: ``[]`` is
present, is a list, and intersects the money floor set in nothing -- so a
REQUIRE_APPROVAL card the governor raised precisely BECAUSE it could not
classify the effect ("unclassifiable effect - gated (dangerous-until-proven)")
became one-tap approvable from a phone.

Two halves, both pinned here:

  * CONSUMER -- ``decision_bridge.classify_resolution`` floors an empty /
    unknown-only tag list. Note that adding ``"unknown"`` to
    ``_FLOOR_EFFECT_TAGS`` would NOT have closed this: an empty list
    intersects nothing.
  * PRODUCER -- ``tool_sandbox._maybe_gate_tool`` stamps the EFFECTIVE tags the
    governor actually scored, not the raw DECLARED ones. Without this a tool
    declaring ``["net_read"]`` whose NAME escalates it to ``money_move``
    stamped ``["net_read"]`` -- non-empty, so the empty-list fix alone does not
    catch it -- and sailed past the money floor.

The integration tests drive the REAL chokepoint (ToolSandbox.execute_tool ->
_maybe_gate_tool -> InboxQueue.enqueue -> OperatorDecisionQueue.post ->
classify_resolution -> a decision persisted on a REAL FileVault) and read the
class back off disk. Hand-built context dicts carrying keys the real producers
never persist are exactly why the previous green suite missed the original
fail-open (see test_rp1_gate_creation_integration.py).
"""
from __future__ import annotations

import asyncio

import pytest

from systemu.messaging.decision_bridge import (
    RESOLUTION_FLOOR,
    RESOLUTION_REMOTE,
    classify_resolution,
)


def _gate_ctx(effect_tags, **over):
    """A tool-gate context shaped exactly like the sandbox's context_extras."""
    ctx = {
        "kind": "gate",
        "gate_type": "tool",
        "verdict": "require_approval",
        "effect_tags": effect_tags,
        "destructive": False,
    }
    ctx.update(over)
    return ctx


# -- CONSUMER pins ------------------------------------------------------------

@pytest.mark.parametrize(
    "tags",
    [
        [],                        # THE BUG: present + a list + disjoint
        [""],                      # a tag that classifies nothing
        ["   "],
        ["unknown"],               # the scorer's own sentinel
        ["UNKNOWN"],               # case-insensitive
        ["unknown", "net_mutate"], # partial classification is not proof
    ],
)
def test_unclassified_effect_tags_floor(tags):
    assert classify_resolution(_gate_ctx(tags)) == RESOLUTION_FLOOR


@pytest.mark.parametrize(
    "tags", [["net_mutate"], ["send_message"], ["local_delete"], ["oauth_call"]]
)
def test_positively_classified_non_money_stays_remotely_resolvable(tags):
    """The fix must not make remote approval useless. A POSITIVELY classified,
    non-money tool gate -- the exact population R-P1 Part C was built to keep on
    the phone -- is still one-tap resolvable."""
    assert classify_resolution(_gate_ctx(tags)) == RESOLUTION_REMOTE


@pytest.mark.parametrize("tags", [["money_move"], ["irreversible"], ["payment"]])
def test_money_and_irreversible_floor_unchanged(tags):
    assert classify_resolution(_gate_ctx(tags)) == RESOLUTION_FLOOR


# -- PRODUCER / REAL-PATH integration -----------------------------------------

def _drive_real_tool_gate(tmp_path, *, tool_name, declared_tags, params):
    """Drive the REAL producer chain; return (persisted_context, vault, dec_id).

    Nothing here is hand-built: the context is whatever ``_maybe_gate_tool``
    actually stamped, round-tripped through ``FileVault.save_decision``.
    """
    from systemu.approval.exceptions import PendingOperatorDecision
    from systemu.core.models import Tool, ToolType
    from systemu.runtime.tool_sandbox import ToolSandbox
    from systemu.storage.file_vault import FileVault
    from systemu.vault.vault import Vault

    impl_dir = tmp_path / "impls"
    impl_dir.mkdir(parents=True, exist_ok=True)
    impl = impl_dir / f"{tool_name}.py"
    impl.write_text("def run():\n    return {'success': True}\n", encoding="utf-8")

    tool = Tool(id=f"tool_{tool_name}", name=tool_name, description="d",
                tool_type=ToolType.PYTHON_FUNCTION,
                implementation_path=str(impl.relative_to(tmp_path)),
                effect_tags=list(declared_tags), version=1)

    vault = FileVault(Vault(str(tmp_path / "v")))
    sandbox = ToolSandbox(str(tmp_path), vault=vault)

    with pytest.raises(PendingOperatorDecision) as exc:
        asyncio.run(sandbox.execute_tool(str(impl), params, tool=tool))
    dec_id = exc.value.decision_id
    return vault.get_decision(dec_id).context, vault, dec_id


def test_real_untagged_money_tool_is_not_phone_approvable(tmp_path):
    """``wire_funds`` declares NO tags. The governor escalates it to
    ``money_move`` by name and cards it -- and ``wire`` is NOT in
    ``is_destructive_call``'s hint list, so nothing else floors the card."""
    ctx, _, _ = _drive_real_tool_gate(
        tmp_path, tool_name="wire_funds", declared_tags=[],
        params={"account": "ACCT-9", "amount": "10000"})

    assert ctx["verdict"] == "require_approval"
    assert ctx["destructive"] is False, "must not be floored by some other axis"
    assert "money_move" in ctx["effect_tags"], "stamp must disclose the scored effect"
    assert ctx["resolution_class"] == RESOLUTION_FLOOR


def test_real_unclassifiable_tool_is_not_phone_approvable(tmp_path):
    """The governor's own reason for this card is 'unclassifiable effect --
    gated (dangerous-until-proven)'. That must not become a phone tap."""
    ctx, _, _ = _drive_real_tool_gate(
        tmp_path, tool_name="sync_records", declared_tags=[],
        params={"path": "/data/x"})

    assert ctx["verdict"] == "require_approval"
    assert ctx["destructive"] is False
    assert ctx["effect_tags"] == ["unknown"]
    assert ctx["resolution_class"] == RESOLUTION_FLOOR


def test_real_declared_tags_cannot_hide_a_name_derived_money_escalation(tmp_path):
    """The PRODUCER half, and the reason an empty-list-only fix is insufficient.

    Declared ``["net_read"]`` is NON-empty and disjoint from the money floor
    set, so the consumer fix alone would let this through -- but the governor
    scored ``money_move``, and the stamp must say what was scored.
    """
    ctx, _, _ = _drive_real_tool_gate(
        tmp_path, tool_name="wire_funds_v2", declared_tags=["net_read"],
        params={"account": "ACCT-9", "amount": "10000"})

    assert ctx["effect_tags"] != ["net_read"], "stamped the DECLARED tags, not the scored ones"
    assert "money_move" in ctx["effect_tags"]
    assert ctx["resolution_class"] == RESOLUTION_FLOOR


def test_real_benign_classified_tool_stays_phone_approvable(tmp_path):
    """R-P1 must not go inert. A positively-classified benign ``net_mutate``
    tool gate is STILL remotely resolvable through the real path -- this is the
    'remote approval becomes useless' objection, pinned."""
    ctx, _, _ = _drive_real_tool_gate(
        tmp_path, tool_name="sync_calendar", declared_tags=["net_mutate"],
        params={"calendar": "work"})

    assert ctx["effect_tags"] == ["net_mutate"]
    assert ctx["resolution_class"] == RESOLUTION_REMOTE


def test_real_untagged_money_tool_refuses_an_actual_phone_tap(tmp_path, monkeypatch):
    """End-to-end ENFORCEMENT: the persisted class is the bit
    ``resolve_from_channel`` reads, so an actual inbound tap must be REFUSED."""
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.messaging import decision_bridge as db

    ctx, vault, dec_id = _drive_real_tool_gate(
        tmp_path, tool_name="charge_card", declared_tags=[],
        params={"card": "tok_x", "amount": "499.00"})
    assert ctx["resolution_class"] == RESOLUTION_FLOOR

    sender = "operator-phone-empty-tags"
    monkeypatch.setattr(db, "_allowlist", lambda: {sender})

    outcome, msg = db.resolve_from_channel(
        db.decision_tag(dec_id), "a2",
        sender_id=sender, channel="telegram",
        queue=OperatorDecisionQueue(vault))

    assert outcome == "REFUSED_TYPED_CONFIRM", outcome
    assert "dashboard" in msg.lower()
    # and the decision is STILL pending -- the tap changed nothing.
    assert vault.get_decision(dec_id).status == "pending"
