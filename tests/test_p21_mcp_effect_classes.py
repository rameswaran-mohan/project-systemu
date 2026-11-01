"""P21 -- an MCP tool's gate card is classified by the SAME effect-class machinery as a
native tool, from what the SERVER declares, failing closed; and a money / irreversible
MCP tool gets exactly the R1 treatment on every surface.

Witnessed END TO END against a REAL MCP server (``tests/_mcp_effects_server.py``, stdio,
both SDK lines through the one compat seam): the server is connected and its tools
discovered + pinned exactly as Settings -> Connectors does, then each tool is driven
through the real chokepoint (``dispatch.call_mcp_tool``) to the card it posts, and the
card's choices are read back off every surface that can resolve it:

  * the dashboard Inbox card model (``inbox_page._inbox_card_model`` over
    ``InboxQueue.list_descriptors``) and its server-side resolve
    (``inbox.resolve_pending_gate``);
  * the CLI ``decisions resolve`` path (``queue.resolve`` + ``inbox.resolve_gate``);
  * the phone lane (``decision_bridge.resolve_from_channel``);
  * the dispatcher handler that records "Always allow" / session trust
    (``mcp_call_gate_handler``) and the standing-approval store itself;
  * the bulk first-gate executor (``first_gate_review.apply_bulk_decision``);
  * the chat lane's re-call after an approval.

The chat-lane ledger row (DEFECT 2) is pinned in ``test_p21_mcp_chat_lane_ledger.py``,
which shares these fixtures.

Mutation map (each mutation turns the named tests red):
  * ignore annotations                 -> test_destructive_hint_alone_refuses_an_unclassified_tool
  * let readOnlyHint downgrade         -> test_read_only_claim_does_not_hide_a_delete
  * drop the MCP branch of classifier  -> every test_*_money_card_* / *_irreversible_*
  * a DENY honours a bypass            -> test_a_refusal_honours_no_approval_of_any_kind
  * reclassify writes nothing          -> test_reclassify_card_saves_an_additive_assignment_then_r1_applies
  * classifier ignores the assignment  -> the same, and test_a_reclassified_call_is_never_frictionless
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

FX_SERVER = str(Path(__file__).resolve().parent / "_mcp_effects_server.py")
SERVER = "fx"


class _Cfg:
    """Minimal config: the chokepoint reads only the result cap off it."""
    mcp_max_result_size_chars = 50_000


@pytest.fixture(scope="module")
def fx_listing(tmp_path_factory):
    """ONE real discovery of the effects server per module (a stdio spawn is seconds
    on Windows). Every test then connects a FRESH vault from this listing exactly as
    ``client.discover_and_pin`` does -- same pin hash, same enable metadata."""
    from systemu.runtime.mcp import connections as conn
    from systemu.runtime.mcp.client import mcp_list_tools
    from systemu.vault.vault import Vault
    scratch = Vault(str(tmp_path_factory.mktemp("p21_discovery") / "vault"))
    conn.add_server(scratch, SERVER, transport=_STDIO)
    listed = mcp_list_tools(server=SERVER, vault=scratch)
    assert listed.get("success") is True, listed
    return {t["name"]: t for t in listed["tools"]}


_STDIO = {"transport": "stdio", "command": sys.executable, "args": [FX_SERVER],
          "env": {}}


@pytest.fixture
def fx(tmp_path, fx_listing):
    """A vault connected to the REAL effects server: pinned, and every tool enabled
    with the annotations and schema the SERVER sent (the Settings path)."""
    import systemu.runtime.command_approvals as ca
    from systemu.runtime.mcp import connections as conn
    from systemu.runtime.mcp.sdk.schema_map import tool_def_hash
    from systemu.vault.vault import Vault

    v = Vault(str(tmp_path / "vault"))
    ca.reset_default_store_for_tests()
    store = ca.init_default_store(tmp_path / "approvals")
    conn.add_server(v, SERVER, transport=_STDIO)
    for t in fx_listing.values():              # == client.discover_and_pin's pin
        conn.set_tool_hash(v, SERVER, t["name"], tool_def_hash(
            name=t["name"], description=t.get("description", ""),
            input_schema=t.get("schema") or {}))
        conn.set_tool_enabled(v, SERVER, t["name"], True,
                              description=t["description"], schema=t["schema"],
                              annotations=t["annotations"])
    try:
        yield SimpleNamespace(vault=v, store=store, tools=dict(fx_listing),
                              tmp=tmp_path)
    finally:
        ca.reset_default_store_for_tests()


def _call(fx, tool, params, *, resolved_dedup=None, gate_out=None, session="quick_p21a"):
    from systemu.runtime.mcp.dispatch import call_mcp_tool
    kw = {"resolved_dedup": resolved_dedup}
    if gate_out is not None:
        kw["gate_out"] = gate_out
    return call_mcp_tool(SERVER, tool, params, vault=fx.vault, config=_Cfg(),
                         session_id=session, **kw)


def _post(fx, tool, params, **kw):
    """Drive ``tool`` to its card; return (PendingOperatorDecision, decision)."""
    from systemu.approval.exceptions import PendingOperatorDecision
    with pytest.raises(PendingOperatorDecision) as exc:
        _call(fx, tool, params, **kw)
    return exc.value, fx.vault.get_decision(exc.value.decision_id)


def _dashboard_card(fx, dec_id):
    """The Inbox page's card model, built the way the page builds it."""
    from systemu.interface.command.inbox import InboxQueue
    from systemu.interface.pages.inbox_page import _inbox_card_model
    for did, desc in InboxQueue(fx.vault).list_descriptors():
        if did == dec_id:
            return desc, _inbox_card_model(desc)
    raise AssertionError(f"card {dec_id} not listed")


def _assert_r1_card(fx, pend, dec, *, effects):
    """The R1 card shape, on the card itself and on the dashboard model."""
    assert pend.options == ["Deny", "Approve once"]
    assert dec.options == ["Deny", "Approve once"]
    ctx = dec.context
    assert ctx["gate_type"] == "mcp_call"
    assert ctx["requires_typed_confirm"] is True
    assert ctx["irreversible"] is True
    for e in effects:
        assert e in ctx["irreversible_effects"], ctx["irreversible_effects"]
        assert e in ctx["effect_tags"], ctx["effect_tags"]
    assert type(ctx["confirm_phrase"]) is str and ctx["confirm_phrase"]
    desc, model = _dashboard_card(fx, dec.id)
    assert "Always allow" not in model["options"]
    assert "Trust this tool for the session" not in model["options"]
    assert model["options"] == ["Deny", "Approve once"]
    assert model["typed_confirm"] == ctx["confirm_phrase"]
    assert model["risk"] == "high"
    for e in effects:
        assert e in model["irreversible_effects"]
    return ctx["confirm_phrase"]


def _assert_deny_card(fx, pend, dec, *, effects=()):
    """The native DENY shape: a REFUSAL. Deny / "Reclassify effect..." only, no
    approval of any kind, verdict deny, floored off the phone."""
    from systemu.interface.command.gate import RECLASSIFY_OPTION
    from systemu.messaging.decision_bridge import RESOLUTION_FLOOR
    assert pend.options == ["Deny", RECLASSIFY_OPTION]
    assert dec.options == ["Deny", RECLASSIFY_OPTION]
    ctx = dec.context
    assert ctx["gate_type"] == "mcp_call" and ctx["verdict"] == "deny"
    assert "unknown" in ctx["effect_tags"]
    for e in effects:
        assert e in ctx["effect_tags"], ctx["effect_tags"]
    assert not ctx.get("requires_typed_confirm")
    assert ctx["resolution_class"] == RESOLUTION_FLOOR
    _desc, model = _dashboard_card(fx, dec.id)
    assert model["options"] == ["Deny", RECLASSIFY_OPTION]
    assert "Approve once" not in model["options"]
    assert model["risk"] == "high"


# ═════════════════════════════════════════════════════════════════════════════
#  1. The cards: money, destructive, read-only-but-destructive, unannotated
# ═════════════════════════════════════════════════════════════════════════════

def test_transfer_funds_money_card_is_typed_confirm_only(fx):
    pend, dec = _post(fx, "transfer_funds", {"to_account": "acct-9", "amount": 500})
    phrase = _assert_r1_card(fx, pend, dec, effects=["money_move"])
    assert phrase == "500"                      # the amount names the action


def test_delete_records_with_destructive_hint_is_irreversible(fx):
    pend, dec = _post(fx, "delete_records", {"record_ids": "1,2"})
    _assert_r1_card(fx, pend, dec, effects=["irreversible", "local_delete"])


def test_destructive_hint_alone_refuses_an_unclassified_tool(fx):
    """``reset_ledger`` carries NO name signal: only the server's destructiveHint
    raises it. UNKNOWN with that high-severity signal is the native DENY floor, so the
    call is REFUSED (Deny / Reclassify only). Ignore annotations and this card offers
    Always allow again."""
    pend, dec = _post(fx, "reset_ledger", {"ledger_id": "L1"})
    _assert_deny_card(fx, pend, dec, effects=["irreversible"])


def test_read_only_claim_does_not_hide_a_delete(fx):
    """``remove_user`` claims readOnlyHint=True, is pinned (classification-trusted),
    and is a delete by name. The claim may not lower it: it is gated, R1."""
    from systemu.runtime.mcp import connections as conn
    assert conn.get_tool_hash(fx.vault, SERVER, "remove_user") is not None
    assert fx.tools["remove_user"]["annotations"].get("readOnlyHint") is True
    pend, dec = _post(fx, "remove_user", {"user_id": "u1"})
    _assert_r1_card(fx, pend, dec, effects=["local_delete"])


def test_unannotated_tool_is_the_unknown_band_and_never_auto_allows(fx):
    pend, dec = _post(fx, "sync_data", {"source": "s3://bucket"})
    ctx = dec.context
    assert ctx["verdict"] == "require_approval"
    assert ctx["effect_tags"] == ["unknown"]
    assert not ctx.get("requires_typed_confirm")
    assert ctx["destructive"] is True            # the per-call tier
    assert dec.options[0] == "Deny"
    # Session trust never covers the UNKNOWN band.
    from systemu.runtime.command_approvals import mcp_session_key
    fx.store.trust_session(mcp_session_key(SERVER, "sync_data", "quick_p21a"),
                           server=SERVER, tool="sync_data", session_id="quick_p21a")
    _post(fx, "sync_data", {"source": "s3://bucket"})


def test_an_honest_read_only_tool_still_runs_ungated(fx):
    """The control: a read-only hint nothing contradicts keeps the shipped no-gate
    behaviour for a pinned tool -- and the pass is recorded as what it was."""
    gate = {}
    out = _call(fx, "lookup_status", {"item": "x"}, gate_out=gate)
    assert out["success"] is True, out
    assert gate["gate"] == "mcp" and gate["passed_via"] == "read_only_hint"
    assert gate["effect_tags"] == ["unknown"]


def test_snake_case_annotation_raises_too(tmp_path, fx):
    """mcp 2.x spells hints snake_case; the classifier reads them through wire_field."""
    from systemu.runtime.mcp import connections as conn
    conn.set_tool_enabled(fx.vault, SERVER, "reset_ledger", True,
                          description="d", schema=fx.tools["reset_ledger"]["schema"],
                          annotations={"destructive_hint": True})
    pend, dec = _post(fx, "reset_ledger", {"ledger_id": "L1"})
    _assert_deny_card(fx, pend, dec, effects=["irreversible"])


# ═════════════════════════════════════════════════════════════════════════════
#  2. Every surface refuses the standing / one-click approvals
# ═════════════════════════════════════════════════════════════════════════════

def test_money_card_standing_approval_refused_by_store_and_handler(fx):
    from systemu.pipelines.mcp_call_gate_handler import _handle_resolved_mcp_call
    from systemu.runtime.command_approvals import mcp_session_key, mcp_signature
    _, dec = _post(fx, "transfer_funds", {"to_account": "a", "amount": 5})
    sig = mcp_signature(SERVER, "transfer_funds")
    assert fx.store.approve(sig, command="mcp:fx:transfer_funds") is False
    assert fx.store.is_approved(sig) is False
    for label in ("Always allow", "Trust this tool for the session"):
        _handle_resolved_mcp_call(SimpleNamespace(
            dedup_key=dec.dedup_key, context=dict(dec.context), choice=label),
            label, None, fx.vault)
    assert fx.store.is_approved(sig) is False
    assert fx.store.is_session_trusted(
        mcp_session_key(SERVER, "transfer_funds", "quick_p21a")) is False


def test_irreversible_card_ignores_a_standing_approval_minted_before_it(fx):
    """An approval on record from before the classification (an older build, a hand
    edit) does not cover an irreversible call: the CONSUMPTION side refuses it."""
    import json
    from systemu.runtime.command_approvals import mcp_signature
    sig = mcp_signature(SERVER, "delete_records")
    raw = json.loads(fx.store.path.read_text()) if fx.store.path.exists() else {}
    raw.setdefault("approved", {})[sig] = {"approved_at": "2026-01-01T00:00:00+00:00",
                                           "approved_by": "operator",
                                           "command": "mcp:fx:delete_records", "cwd": ""}
    fx.store.path.parent.mkdir(parents=True, exist_ok=True)
    fx.store.path.write_text(json.dumps(raw))
    pend, dec = _post(fx, "delete_records", {"record_ids": "9"})
    _assert_r1_card(fx, pend, dec, effects=["local_delete"])


def test_irreversible_card_ignores_session_trust(fx):
    from systemu.runtime.command_approvals import mcp_session_key
    fx.store.trust_session(mcp_session_key(SERVER, "delete_records", "quick_p21a"),
                           server=SERVER, tool="delete_records", session_id="quick_p21a")
    pend, dec = _post(fx, "delete_records", {"record_ids": "1"})
    _assert_r1_card(fx, pend, dec, effects=["irreversible"])


def test_money_card_bulk_refused(fx):
    from systemu.runtime.command_approvals import mcp_signature
    from systemu.runtime.first_gate_review import (OPT_BULK_ALLOW, ReviewEntry,
                                                   apply_bulk_decision)
    _, dec = _post(fx, "transfer_funds", {"to_account": "a", "amount": 5})
    sig = mcp_signature(SERVER, "transfer_funds")
    honest = ReviewEntry(name="transfer_funds", signature=sig,
                         effect_tags=tuple(dec.context["effect_tags"]))
    assert apply_bulk_decision([honest], choice=OPT_BULK_ALLOW, store=fx.store) == []
    # A bulk entry that MIS-labels the signature as a batchable class still cannot
    # mint a standing approval: the store refuses the gate-marked signature.
    stale = ReviewEntry(name="lookup", signature=sig, effect_tags=("clipboard_read",))
    apply_bulk_decision([stale], choice=OPT_BULK_ALLOW, store=fx.store)
    assert fx.store.is_approved(sig) is False


def test_money_card_phone_refused(fx, monkeypatch):
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.messaging import decision_bridge as db
    _, dec = _post(fx, "transfer_funds", {"to_account": "a", "amount": 5})
    assert dec.context["resolution_class"] == db.RESOLUTION_FLOOR
    monkeypatch.setattr(db, "_allowlist", lambda: {"42"})
    q = OperatorDecisionQueue(fx.vault)
    for key in ("a1", "a2"):
        outcome, _msg = db.resolve_from_channel(db.decision_tag(dec.id), key,
                                                sender_id="42", channel="telegram",
                                                queue=q)
        assert outcome == "REFUSED_TYPED_CONFIRM"
    assert fx.vault.get_decision(dec.id).status == "pending"


def test_money_card_cli_resolve_without_phrase_runs_nothing(fx):
    """The CLI's own two calls: ``queue.resolve`` then ``inbox.resolve_gate``. A
    standing label is not even an option; "Approve once" without the typed phrase is
    refused, the card goes back to the Inbox, and the chat-lane re-call runs nothing."""
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.approval.exceptions import PendingOperatorDecision
    from systemu.interface.command.inbox import resolve_gate
    pend, dec = _post(fx, "transfer_funds", {"to_account": "a", "amount": 5})
    q = OperatorDecisionQueue(fx.vault)
    with pytest.raises(ValueError):
        q.resolve(dec.id, choice="Always allow")
    resolved = q.resolve(dec.id, choice="Approve once")
    res = resolve_gate(resolved, vault=fx.vault)
    assert res.status.value == "noop"
    assert res.data.get("typed_confirm_refused") is True
    assert "Not approved" in res.summary
    assert fx.vault.get_decision(dec.id).status == "pending"
    # Even had the lane seen the bare click first, the gate would not run it.
    q.resolve(dec.id, choice="Approve once")
    with pytest.raises(PendingOperatorDecision):
        _call(fx, "transfer_funds", {"to_account": "a", "amount": 5},
              resolved_dedup=pend.dedup_key)


def test_money_card_dashboard_needs_the_exact_phrase_then_runs_once(fx):
    from systemu.approval.exceptions import PendingOperatorDecision
    from systemu.interface.command.inbox import resolve_pending_gate
    params = {"to_account": "acct-9", "amount": 500}
    pend, dec = _post(fx, "transfer_funds", params)
    for typed in (None, "", "50", "transfer_funds"):
        res = resolve_pending_gate(dec.id, choice="Approve once", vault=fx.vault,
                                   typed_confirm=typed)
        assert res.data.get("typed_confirm_refused") is True
        assert fx.vault.get_decision(dec.id).status == "pending"
    resolve_pending_gate(dec.id, choice="Approve once", vault=fx.vault,
                         typed_confirm="500")
    gate = {}
    out = _call(fx, "transfer_funds", params, resolved_dedup=pend.dedup_key,
                gate_out=gate)
    assert out["success"] is True, out
    assert gate["passed_via"] == "approve_once" and gate["decision_id"] == dec.id
    assert "money_move" in gate["effect_tags"]
    # One typed confirmation buys ONE call: the next identical call is gated again.
    with pytest.raises(PendingOperatorDecision):
        _call(fx, "transfer_funds", params, resolved_dedup=pend.dedup_key)


def test_typed_approval_does_not_cash_a_different_amount(fx):
    from systemu.approval.exceptions import PendingOperatorDecision
    from systemu.interface.command.inbox import resolve_pending_gate
    pend, dec = _post(fx, "transfer_funds", {"to_account": "a", "amount": 5})
    resolve_pending_gate(dec.id, choice="Approve once", vault=fx.vault,
                         typed_confirm="5")
    with pytest.raises(PendingOperatorDecision):
        _call(fx, "transfer_funds", {"to_account": "a", "amount": 5000},
              resolved_dedup=pend.dedup_key)


# ═════════════════════════════════════════════════════════════════════════════
#  3. The operator's declaration (connect-time, operator-controlled)
# ═════════════════════════════════════════════════════════════════════════════

def test_operator_declaration_raises_a_benign_tool_to_money(fx):
    from systemu.runtime.mcp import connections as conn
    conn.set_operator_effect_classes(fx.vault, SERVER, "lookup_status", ["money_move"])
    pend, dec = _post(fx, "lookup_status", {"item": "x"})
    _assert_r1_card(fx, pend, dec, effects=["money_move"])


def test_operator_declaration_survives_other_connection_writes(fx):
    from systemu.runtime.mcp import connections as conn
    conn.set_operator_effect_classes(fx.vault, SERVER, "lookup_status", ["payment"])
    conn.set_tool_hash(fx.vault, SERVER, "lookup_status", "deadbeef")
    conn.set_tool_enabled(fx.vault, SERVER, "sync_data", False)
    assert conn.get_operator_effect_classes(fx.vault, SERVER, "lookup_status") == (
        "payment",)


def test_operator_may_lower_a_server_destructive_hint(fx):
    """Lowering is the operator's call, on the operator's surface: a declared class
    replaces the server's hints. ``net_mutate`` -> an ordinary approval card."""
    from systemu.runtime.mcp import connections as conn
    conn.set_operator_effect_classes(fx.vault, SERVER, "reset_ledger", ["net_mutate"])
    pend, dec = _post(fx, "reset_ledger", {"ledger_id": "L1"})
    assert not dec.context.get("requires_typed_confirm")
    assert dec.context["effect_tags"] == ["net_mutate"]
    assert "Always allow" in dec.options


def test_server_data_cannot_write_the_operator_declaration(fx):
    """The declaration is the operator's: enabling a tool with whatever the server
    sent (here a forged ``operator_effect_classes`` riding in its annotations and
    schema) leaves it untouched, and the hints still only raise."""
    from systemu.runtime.mcp import connections as conn
    conn.set_tool_enabled(fx.vault, SERVER, "reset_ledger", True, description="d",
                          schema={"operator_effect_classes": ["no_effect"]},
                          annotations={"destructiveHint": True,
                                       "operator_effect_classes": ["no_effect"]})
    assert conn.get_operator_effect_classes(fx.vault, SERVER, "reset_ledger") is None
    assert conn.get_operator_assigned_class(fx.vault, SERVER, "reset_ledger") is None
    pend, dec = _post(fx, "reset_ledger", {"ledger_id": "L1"})
    _assert_deny_card(fx, pend, dec, effects=["irreversible"])


# ═════════════════════════════════════════════════════════════════════════════
#  4. DENY refuses (native parity), and the MCP reclassify card
# ═════════════════════════════════════════════════════════════════════════════

def test_a_refusal_honours_no_approval_of_any_kind(fx):
    """A DENY-scored call never runs on a standing approval, a session trust, or a
    resolved one-shot decision -- each bypass sits under ``not deny``."""
    import json

    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.interface.command.gate import RECLASSIFY_OPTION
    from systemu.runtime.command_approvals import mcp_session_key, mcp_signature
    sig = mcp_signature(SERVER, "reset_ledger")
    raw = json.loads(fx.store.path.read_text()) if fx.store.path.exists() else {}
    raw.setdefault("approved", {})[sig] = {"approved_at": "2026-01-01T00:00:00+00:00",
                                           "approved_by": "operator",
                                           "command": "mcp:fx:reset_ledger", "cwd": ""}
    raw.setdefault("session_trusted", {})[
        mcp_session_key(SERVER, "reset_ledger", "quick_p21a")] = {
        "trusted_at": "2026-01-01T00:00:00+00:00"}
    fx.store.path.parent.mkdir(parents=True, exist_ok=True)
    fx.store.path.write_text(json.dumps(raw))
    pend, dec = _post(fx, "reset_ledger", {"ledger_id": "L1"})
    _assert_deny_card(fx, pend, dec)
    # A resolved answer on the card buys nothing either: the call is still refused.
    OperatorDecisionQueue(fx.vault).resolve(dec.id, choice=RECLASSIFY_OPTION)
    pend2, dec2 = _post(fx, "reset_ledger", {"ledger_id": "L1"},
                        resolved_dedup=pend.dedup_key)
    _assert_deny_card(fx, pend2, dec2)


def test_reclassify_card_saves_an_additive_assignment_then_r1_applies(fx):
    """The operator assigns the class on the dashboard Inbox (typed). It is saved as
    the operator's ASSIGNMENT for the tool (connections.set_operator_effect_classes),
    the card resolves as a reclassify, nothing runs -- and the next call is scored
    with it ADDED: the server's destructive hint still makes it irreversible, so the
    follow-up is the R1 typed card, never a frictionless run."""
    from systemu.interface.command.gate import RECLASSIFY_OPTION
    from systemu.interface.command.inbox import resolve_pending_gate
    from systemu.interface.pages.inbox_page import _resolve_mcp_reclassify
    from systemu.runtime.mcp import connections as conn
    pend, dec = _post(fx, "reset_ledger", {"ledger_id": "L1"})
    message, kind = _resolve_mcp_reclassify(dec.id, "local_write", "local_write",
                                            vault=fx.vault)
    assert kind == "positive", message
    assert conn.get_operator_assigned_class(fx.vault, SERVER, "reset_ledger") == (
        "local_write")
    assert conn.get_operator_effect_classes(fx.vault, SERVER, "reset_ledger") is None
    after = fx.vault.get_decision(dec.id)
    assert after.status == "resolved" and after.choice == RECLASSIFY_OPTION
    assert after.context["assigned_class"] == "local_write"
    pend2, dec2 = _post(fx, "reset_ledger", {"ledger_id": "L1"})
    assert dec2.id != dec.id
    assert dec2.context["verdict"] == "require_approval"
    assert "local_write" in dec2.context["effect_tags"]
    phrase = _assert_r1_card(fx, pend2, dec2, effects=["irreversible"])
    resolve_pending_gate(dec2.id, choice="Approve once", vault=fx.vault,
                         typed_confirm=phrase)
    gate = {}
    out = _call(fx, "reset_ledger", {"ledger_id": "L1"},
                resolved_dedup=pend2.dedup_key, gate_out=gate)
    assert out["success"] is True, out
    assert gate["passed_via"] == "approve_once" and gate["decision_id"] == dec2.id


@pytest.mark.parametrize("selected,typed", [("local_write", "Local_write"),
                                            ("local_write", ""),
                                            ("local_write", "local_write "),
                                            ("unknown", "unknown"),
                                            ("no_effect", "no_effect"),
                                            ("", "")])
def test_reclassify_records_nothing_without_the_exact_typed_class(fx, selected, typed):
    from systemu.interface.pages.inbox_page import _resolve_mcp_reclassify
    from systemu.runtime.mcp import connections as conn
    _pend, dec = _post(fx, "reset_ledger", {"ledger_id": "L1"})
    _message, kind = _resolve_mcp_reclassify(dec.id, selected, typed, vault=fx.vault)
    assert kind != "positive"
    assert conn.get_operator_assigned_class(fx.vault, SERVER, "reset_ledger") is None
    assert fx.vault.get_decision(dec.id).status == "pending"


def test_reclassify_refuses_a_card_that_is_not_a_refusal(fx):
    from systemu.interface.pages.inbox_page import _resolve_mcp_reclassify
    from systemu.runtime.mcp import connections as conn
    _pend, dec = _post(fx, "transfer_funds", {"to_account": "a", "amount": 5})
    _message, kind = _resolve_mcp_reclassify(dec.id, "local_write", "local_write",
                                             vault=fx.vault)
    assert kind != "positive"
    assert conn.get_operator_assigned_class(fx.vault, SERVER, "transfer_funds") is None
    assert fx.vault.get_decision(dec.id).status == "pending"


def test_a_reclassified_call_is_never_frictionless(fx):
    """``sync_data`` with a destructive parameter is UNKNOWN + a high-severity signal:
    refused. Assigning even a read class lifts the refusal to an approval card -- the
    governor's IMPL-2 floor never lets it run unasked."""
    from systemu.interface.pages.inbox_page import _resolve_mcp_reclassify
    params = {"source": "rm -rf /data"}
    pend, dec = _post(fx, "sync_data", params)
    _assert_deny_card(fx, pend, dec)
    _message, kind = _resolve_mcp_reclassify(dec.id, "local_read", "local_read",
                                             vault=fx.vault)
    assert kind == "positive"
    for p in (params, {"source": "s3://bucket"}):
        _pend2, dec2 = _post(fx, "sync_data", p)
        assert dec2.context["verdict"] == "require_approval"
        assert "local_read" in dec2.context["effect_tags"]


def test_chat_lane_reports_a_reclassify_without_running_the_call(fx, monkeypatch):
    from systemu.interface.command.gate import RECLASSIFY_OPTION
    from systemu.pipelines import quick_task as qt
    monkeypatch.setattr(qt, "_poll_command_choice",
                        lambda vault, dedup_key, timeout=None: RECLASSIFY_OPTION)
    res = qt._execute_mcp_tool({"server": SERVER, "name": "reset_ledger"},
                               {"ledger_id": "L1"}, _Cfg(), vault=fx.vault,
                               session_id="quick_p21r")
    assert res.success is False
    assert "refused and did not run" in res.error
    assert "timed out" not in res.error


def test_a_refusal_honours_no_standing_approval_even_when_reversible(fx):
    """A DENY that is NOT irreversible -- ``sync_data`` (unannotated, UNKNOWN) with a
    destructive parameter -- is still refused over an "Always allow" on record: the
    refusal itself, not R1, is what closes the standing pass."""
    from systemu.runtime.command_approvals import mcp_signature
    sig = mcp_signature(SERVER, "sync_data")
    assert fx.store.approve(sig, command="mcp:fx:sync_data") is True
    pend, dec = _post(fx, "sync_data", {"source": "rm -rf /data"})
    _assert_deny_card(fx, pend, dec)
    assert not dec.context.get("irreversible")
    assert "irreversible" not in dec.context["effect_tags"]
