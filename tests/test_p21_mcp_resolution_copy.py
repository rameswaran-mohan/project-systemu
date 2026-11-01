"""P21 / residual (b) -- resolving an MCP card says what the choice DID.

``inbox.resolve_gate`` executes nothing for an ``mcp_call`` card (the waiting call
re-reads the choice), and it used to fall through to "Gate mcp_call not approved
(<choice>)" -- including right after an operator TYPED the phrase and approved a
money call once. That is false operator copy (DEC-34c). Each sentence below is minted
from a fixed property of the gate or a WITNESSED fact (the approval store, the
connections file), never assumed.

Real cards, posted by the real MCP gate (``dispatch._gate_mcp_call``) into a real vault
and resolved through the dashboard Inbox's own server-side path
(``inbox.resolve_pending_gate``). No MCP server is needed: the gate posts before any
transport is touched.

Mutation: drop the ``mcp_call`` branch from ``resolve_gate`` and every test here goes
red on "not approved".
"""
from __future__ import annotations

import pytest


@pytest.fixture
def gate_env(tmp_path):
    import systemu.runtime.command_approvals as ca
    from systemu.runtime.mcp import connections as conn
    from systemu.vault.vault import Vault
    v = Vault(str(tmp_path / "vault"))
    ca.reset_default_store_for_tests()
    store = ca.init_default_store(tmp_path / "approvals")
    for name, ann in (("transfer_funds", {}), ("sync_data", {}),
                      ("reset_ledger", {"destructiveHint": True})):
        conn.set_tool_enabled(v, "srv", name, True, description="d",
                              schema={"type": "object", "properties": {}},
                              annotations=ann)
        conn.set_tool_hash(v, "srv", name, "pinned")
    try:
        yield v, store
    finally:
        ca.reset_default_store_for_tests()


def _card(v, tool, params, session="quick_p21c"):
    from systemu.approval.exceptions import PendingOperatorDecision
    from systemu.runtime.mcp import dispatch
    with pytest.raises(PendingOperatorDecision) as exc:
        dispatch._gate_mcp_call("srv", tool, params, vault=v, config=None,
                                session_id=session)
    return v.get_decision(exc.value.decision_id)


def test_a_typed_money_approval_is_reported_as_what_it_is(gate_env):
    from systemu.interface.command.inbox import resolve_pending_gate
    v, _store = gate_env
    dec = _card(v, "transfer_funds", {"to": "a", "amount": 40})
    res = resolve_pending_gate(dec.id, choice="Approve once", vault=v,
                               typed_confirm="40")
    text = res.summary.lower()
    assert "not approved" not in text, res.summary
    assert "recorded (approve once): mcp tool call: transfer_funds" in text
    assert "these exact arguments" in text and "nothing is remembered" in text
    assert res.data["notice_kind"] == "positive"
    assert res.summary.isascii()


def test_an_ordinary_chat_approval_names_the_waiting_chat(gate_env):
    from systemu.interface.command.inbox import resolve_pending_gate
    v, _store = gate_env
    dec = _card(v, "sync_data", {"source": "s3://b"})
    res = resolve_pending_gate(dec.id, choice="Approve once", vault=v)
    assert "not approved" not in res.summary.lower()
    assert "the chat waiting on this card runs the call once" in res.summary


def test_a_task_lane_one_time_approval_does_not_promise_a_resume(gate_env):
    from systemu.interface.command.inbox import resolve_pending_gate
    v, _store = gate_env
    dec = _card(v, "sync_data", {"source": "s3://b"}, session="exec_task_1")
    res = resolve_pending_gate(dec.id, choice="Approve once", vault=v)
    assert "not approved" not in res.summary.lower()
    assert "re-running it asks again" in res.summary
    assert res.data["notice_kind"] == "info"


def test_a_deny_says_the_call_does_not_run(gate_env):
    from systemu.interface.command.inbox import resolve_pending_gate
    v, _store = gate_env
    dec = _card(v, "transfer_funds", {"to": "a", "amount": 40})
    res = resolve_pending_gate(dec.id, choice="Deny", vault=v)
    assert res.summary.startswith("Recorded (Deny): MCP tool call: transfer_funds")
    assert "does not run" in res.summary
    assert "approved" not in res.summary.lower()
    assert res.data["notice_kind"] == "negative"


def test_always_allow_is_claimed_as_remembered_only_when_it_is(gate_env):
    """From the Inbox page nothing writes the standing approval, and the sentence
    says so; once the dispatcher handler has written it, the sentence says that."""
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.interface.command.inbox import resolve_gate, resolve_pending_gate
    from systemu.pipelines.mcp_call_gate_handler import _handle_resolved_mcp_call
    v, store = gate_env
    dec = _card(v, "sync_data", {"source": "s3://b"})
    res = resolve_pending_gate(dec.id, choice="Always allow", vault=v)
    assert "not approved" not in res.summary.lower()
    assert "no standing approval was recorded" in res.summary
    _handle_resolved_mcp_call(v.get_decision(dec.id), "Always allow", None, v)
    res2 = resolve_gate(v.get_decision(dec.id), vault=v)
    assert "this tool now runs without asking" in res2.summary
    assert res2.data["notice_kind"] == "positive"
    assert OperatorDecisionQueue(v).get_resolved_choice(dec.dedup_key) == "Always allow"


def test_a_reclassify_names_the_saved_class_only_when_saved(gate_env):
    from systemu.interface.command.gate import RECLASSIFY_OPTION
    from systemu.interface.command.inbox import resolve_gate
    from systemu.interface.pages.inbox_page import _resolve_mcp_reclassify
    v, _store = gate_env
    dec = _card(v, "reset_ledger", {"ledger_id": "L1"})
    assert dec.options == ["Deny", RECLASSIFY_OPTION]
    message, kind = _resolve_mcp_reclassify(dec.id, "local_write", "local_write",
                                            vault=v)
    assert kind == "positive", message
    res = resolve_gate(v.get_decision(dec.id), vault=v)
    assert "nothing ran" in res.summary
    assert "local_write is saved as your assignment" in res.summary
    assert "not approved" not in res.summary.lower()
