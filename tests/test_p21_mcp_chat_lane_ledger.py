"""P21 / DEFECT 2 -- a chat-lane MCP call's ledger row carries the MCP gate's stamp.

Before P21 the quick lane's MCP result carried no ``gate`` record, so its action-audit
row (``audit_log.append_action`` -- the one writer the workflow lane uses too) had no
stamp and every gate column of its ledger row read the generic ``not_recorded``
reason. The MCP gate now returns its record of the pass and the chat lane writes it.

Driven through the REAL quick lane against the REAL effects server (fixtures shared
with ``tests/test_p21_mcp_effect_classes.py``).

Mutation: drop ``gate=`` from ``quick_task._execute_mcp_tool``'s result and
``test_chat_lane_mcp_row_carries_the_gate_stamp`` goes red.
"""
from __future__ import annotations

from test_p21_mcp_effect_classes import _Cfg, fx, fx_listing  # noqa: F401 (fixtures)


def test_chat_lane_mcp_row_carries_the_gate_stamp(fx, monkeypatch):
    """Drive the REAL quick lane: a read-only lookup (no card) and a money-moving
    ``settle_account`` the operator approves on the dashboard with the typed phrase.
    Both ledger rows carry the gate's stamp -- verdict, pass path, effect classes, the
    decision id -- through the same writer the workflow lane uses
    (``audit_log.append_action``), and the export says so.

    (``transfer_funds`` is not driven here on purpose: the chat lane's own
    ``_safety_denied`` auto-denies a destructive-NAMED call before it reaches any
    gate, so it never writes an effect row at all.)"""
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.interface.command.inbox import resolve_pending_gate
    from systemu.pipelines import quick_task as qt
    from systemu.runtime import ledger

    seen = {}

    def _operator(vault, dedup_key, timeout=None):
        pending = [d for d in OperatorDecisionQueue(vault).list_pending()
                   if d.dedup_key == dedup_key]
        assert pending, dedup_key
        dec = pending[0]
        seen["card"] = dec
        res = resolve_pending_gate(dec.id, choice="Approve once", vault=vault,
                                   typed_confirm=dec.context["confirm_phrase"])
        assert not res.data.get("typed_confirm_refused"), res.summary
        return "Approve once"
    monkeypatch.setattr(qt, "_poll_command_choice", _operator)

    script = iter([
        {"action": "TOOL_CALL", "tool": "lookup_status", "params": {"item": "x"}},
        {"action": "TOOL_CALL", "tool": "settle_account",
         "params": {"account_id": "A1"}},
        {"action": "ANSWER", "answer_md": "done"},
    ])
    res = qt.run_quick_task("settle the account", _Cfg(), fx.vault,
                            llm_json=lambda **kw: next(script),
                            synthesize=lambda *a, **k: "done")
    assert res.status == "success", res
    card = seen["card"]
    assert card.context["requires_typed_confirm"] is True     # it WAS the R1 card

    rows = ledger.project(fx.vault, data_dir=fx.tmp)
    effects = {r.action.get("tool"): r for r in rows if r.event_kind == "effect"}
    look, settle = effects["lookup_status"], effects["settle_account"]

    assert look.gate.get("gate_kind") == "mcp"
    assert look.gate.get("verdict") == "require_approval"
    assert look.gate.get("passed_via") == "read_only_hint"
    assert look.gate.get("resolved_via") == "auto:read_only_hint"
    assert look.action.get("effect_tags") == ["unknown"]

    assert settle.gate.get("gate_kind") == "mcp"
    assert settle.gate.get("verdict") == card.context["verdict"]
    assert settle.gate.get("passed_via") == "approve_once"
    assert settle.gate.get("decision_id") == card.id
    assert settle.action.get("effect_tags") == ["money_move"]
    assert settle.action.get("effect_tags_basis") == "scored"
    decided = [r for r in rows if r.event_kind == "gate_decision"]
    assert [r.source_id for r in decided] == [card.id]
    for row in (look, settle):
        assert "verdict" not in (row.gate.get(ledger.NOT_RECORDED_KEY) or {})
        assert "effect_tags" not in (row.action.get(ledger.NOT_RECORDED_KEY) or {})
    blob = ledger.export_jsonl(rows).decode()
    assert '"passed_via":"approve_once"' in blob
    assert '"passed_via":"read_only_hint"' in blob
    cells = {r.action.get("tool"): dict(zip(ledger._COLUMNS, ledger._csv_cells(r)))
             for r in rows if r.event_kind == "effect"}
    assert cells["settle_account"]["gate_decision_id"] == card.id
    assert cells["settle_account"]["gate_verdict"] != ledger.NOT_RECORDED
