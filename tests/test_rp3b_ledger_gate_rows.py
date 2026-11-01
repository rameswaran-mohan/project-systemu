"""R-P3b -- the gate verdict, the operator decision and the resolving channel.

Companion to tests/test_rp3b_ledger_rows.py (effect classes + committed effects).
These pins hold the gate half of the row:

  * an effect row's gate_verdict / gate_decision_id / gate_resolved_via come from
    the stamp the sandbox gate wrote when it let the call run -- never inferred;
  * the resolving channel is joined by EXACT decision id from the only two witnesses
    that exist (phone resolutions, policy auto-grants), and is named not_recorded
    everywhere else;
  * every DECIDED gate card is a gate_decision row carrying only fixed-vocabulary
    fields -- never the card's command text, argument preview, phrase or a free-text
    answer;
  * a field that does not apply to a row kind says not_applicable, never blank.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.approval.decision_queue import OperatorDecision, OperatorDecisionQueue
from systemu.approval.exceptions import PendingOperatorDecision
from systemu.core.models import Tool, ToolType
from systemu.runtime import audit_log, ledger, ledger_export, receipts_store
from systemu.runtime.command_approvals import CommandApprovalStore
from systemu.runtime.tool_sandbox import ToolSandbox


def _vault(tmp_path):
    from systemu.vault.vault import Vault
    return Vault(str(tmp_path / "vault"))


def _cells(row):
    return dict(zip(ledger._COLUMNS, ledger._csv_cells(row)))


def _stamp(passed_via, *, decision_id=None, verdict="require_approval", gate="tool"):
    return {"gate": gate, "verdict": verdict, "passed_via": passed_via,
            "decision_id": decision_id, "effect_tags": ["net_mutate"],
            "effect_tags_basis": "scored"}


def _act(v, eid, gate=None, ts_tick=None):
    audit_log.append_action(v, execution_id=eid, objective_id=0, action="push_record",
                            params={"target": "x"}, success=True, gate=gate)


def _phone_ok(v, decision_id, channel="telegram"):
    """The REAL phone-bridge audit writer (decision_bridge._audit)."""
    from systemu.messaging import decision_bridge
    decision_bridge._audit(OperatorDecisionQueue(v), {
        "ts": 0, "channel": channel, "sender_id": "1", "decision_id": decision_id,
        "tag": "x", "choice": "1", "outcome": "OK"})


def _effect(rows, eid):
    return [r for r in rows if r.event_kind == "effect"
            and r.actor["run_ref"]["execution_id"] == eid][0]


# ═════════════════════════════════════════════════════════════════════════════
#  1. effect rows: the gate columns come from the stamp
# ═════════════════════════════════════════════════════════════════════════════

def test_allow_band_pass_has_no_decision_and_an_auto_channel(tmp_path):
    v = _vault(tmp_path)
    _act(v, "exec_a", _stamp("auto_allow", verdict="allow"))
    c = _cells(_effect(ledger.project(v, data_dir=tmp_path), "exec_a"))
    assert (c["gate_verdict"], c["gate_decision_id"], c["gate_resolved_via"]) == (
        "allow", ledger.NOT_APPLICABLE, "auto:allow")


def test_standing_approval_names_its_missing_decision_id(tmp_path):
    v = _vault(tmp_path)
    _act(v, "exec_s", _stamp("standing_approval"))
    row = _effect(ledger.project(v, data_dir=tmp_path), "exec_s")
    c = _cells(row)
    assert (c["gate_verdict"], c["gate_decision_id"], c["gate_resolved_via"]) == (
        "require_approval", ledger.NOT_RECORDED, "auto:standing_approval")
    assert "standing approval" in row.gate["not_recorded_because"]["decision_id"]


def test_resume_bridge_pass_records_neither_decision_nor_channel(tmp_path):
    v = _vault(tmp_path)
    _act(v, "exec_b", _stamp("resume_bridge"))
    c = _cells(_effect(ledger.project(v, data_dir=tmp_path), "exec_b"))
    assert c["gate_decision_id"] == c["gate_resolved_via"] == ledger.NOT_RECORDED


def test_approve_once_joins_the_phone_channel_by_exact_decision_id(tmp_path):
    v = _vault(tmp_path)
    _act(v, "exec_p", _stamp("approve_once", decision_id="dec_phone001"))
    _act(v, "exec_d", _stamp("approve_once", decision_id="dec_dash0001"))
    _phone_ok(v, "dec_phone001")
    rows = ledger.project(v, data_dir=tmp_path)
    phone, dash = _cells(_effect(rows, "exec_p")), _cells(_effect(rows, "exec_d"))
    assert (phone["gate_decision_id"], phone["gate_resolved_via"]) == (
        "dec_phone001", "telegram")
    # no witness for the dashboard/CLI surface: named, never guessed as 'dashboard'
    assert (dash["gate_decision_id"], dash["gate_resolved_via"]) == (
        "dec_dash0001", ledger.NOT_RECORDED)


def test_a_refused_phone_attempt_is_not_a_channel_witness(tmp_path):
    from systemu.messaging import decision_bridge
    v = _vault(tmp_path)
    _act(v, "exec_r", _stamp("approve_once", decision_id="dec_refused1"))
    decision_bridge._audit(OperatorDecisionQueue(v), {
        "ts": 0, "channel": "telegram", "decision_id": "dec_refused1",
        "outcome": "REFUSED_TYPED_CONFIRM"})
    c = _cells(_effect(ledger.project(v, data_dir=tmp_path), "exec_r"))
    assert c["gate_resolved_via"] == ledger.NOT_RECORDED


def test_unstamped_effect_row_names_all_three_gate_columns(tmp_path):
    """DEC-27 -- the misreading the old manifest note guarded against ('blank means no
    approval was required') is now prevented in the cell itself."""
    v = _vault(tmp_path)
    _act(v, "exec_u")
    row = _effect(ledger.project(v, data_dir=tmp_path), "exec_u")
    c = _cells(row)
    assert c["gate_verdict"] == c["gate_decision_id"] == c["gate_resolved_via"] \
        == ledger.NOT_RECORDED
    assert "no gate stamp" in row.gate["not_recorded_because"]["verdict"]


def test_committed_effect_gate_columns_are_not_applicable(tmp_path):
    v = _vault(tmp_path)
    _act(v, "exec_c", _stamp("auto_allow", verdict="allow"))
    receipts_store.write_receipt("exec_c", 0, {
        "objective_id": 0, "confirmed": True, "method": "api_readback",
        "detail": "row 7 created", "stamped_at": "2026-10-01T10:00:00Z",
        "shadow": False}, data_dir=tmp_path)
    committed = [r for r in ledger.project(v, data_dir=tmp_path)
                 if r.event_kind == "committed_effect"][0]
    c = _cells(committed)
    assert c["gate_verdict"] == c["gate_decision_id"] == c["gate_resolved_via"] \
        == ledger.NOT_APPLICABLE


# ═════════════════════════════════════════════════════════════════════════════
#  2. gate_decision rows from the decision queue
# ═════════════════════════════════════════════════════════════════════════════

def _impl_tool(tmp_path, name, tags=()):
    impl_dir = tmp_path / "impls"
    impl_dir.mkdir(parents=True, exist_ok=True)
    impl = impl_dir / f"{name}.py"
    impl.write_text("def run(target=None, command=None, cwd=None):\n"
                    f"    # {name}\n    return {{'success': True}}\n", encoding="utf-8")
    return Tool(id=f"tool_{name}", name=name, description="d",
                tool_type=ToolType.PYTHON_FUNCTION,
                implementation_path=str(impl.relative_to(tmp_path)),
                effect_tags=list(tags), version=1)


def _sandbox(tmp_path, v):
    root = tmp_path / "sb"
    root.mkdir(parents=True, exist_ok=True)
    from sharing_on.config import Config
    return ToolSandbox(str(root), vault=v, config=Config(),
                       command_approvals=CommandApprovalStore(tmp_path / "ca.json"))


def test_real_gate_card_to_decision_row_to_linked_effect_row(tmp_path, monkeypatch):
    """Production path end to end: the gate posts a card (with the run's coords), the
    operator approves once, the chat-lane re-call passes, the action-tool hook writes
    the stamped row -- and the export holds BOTH rows, linked by the decision id."""
    import systemu.runtime.chat_submission_ctx as csc
    monkeypatch.setattr(csc, "current_execution_id", lambda: "quick_gate1", raising=False)
    v = _vault(tmp_path)
    sb = _sandbox(tmp_path, v)
    tool = _impl_tool(tmp_path, "consolidate_records")
    tool.is_action_tool = True
    secret_arg = "ghp_" + "K9" * 160                     # longer than every preview cap
    params = {"target": secret_arg}
    with pytest.raises(PendingOperatorDecision) as exc:
        asyncio.run(sb.execute_tool(tool.implementation_path, params, tool=tool))
    card = OperatorDecisionQueue(v).list_pending()[0]
    OperatorDecisionQueue(v).resolve(card.id, choice="Approve once")
    res = asyncio.run(sb.execute_tool(tool.implementation_path, params, tool=tool,
                                      _command_gate_resolved=exc.value.dedup_key))
    assert res.success, res.error
    sb._after_successful_call(tool=tool, params=params, execution_id="quick_gate1",
                              objective_id=0, gate=res.gate)

    rows = ledger.project(v, data_dir=tmp_path)
    dec = [r for r in rows if r.event_kind == "gate_decision"]
    assert len(dec) == 1
    d = _cells(dec[0])
    assert d["source_kind"] == "decision" and d["source_id"] == card.id
    assert d["gate_decision_id"] == card.id
    assert d["gate_verdict"] == "require_approval"
    assert d["action_tool"] == "consolidate_records"
    assert d["action_effect_tags"] == "unknown"
    assert d["action_params_digest"] == ledger.NOT_RECORDED
    assert d["outcome_status"] == "Approve once"          # the operator's decision
    assert d["actor_lane"] == "quick" and d["actor_origin"] == "chat"
    assert d["outcome_verification"] == ledger.NOT_APPLICABLE
    eff = _cells(_effect(rows, "quick_gate1"))
    assert eff["gate_decision_id"] == card.id               # the witnessed link
    for blob in (ledger.export_csv(rows), ledger.export_jsonl(rows)):
        assert secret_arg.encode() not in blob
        assert secret_arg[:40].encode() not in blob


def test_open_cards_are_not_rows_and_expired_ones_say_so(tmp_path):
    v = _vault(tmp_path)
    q = OperatorDecisionQueue(v)
    q.post(title="t", body="b", options=["Deny", "Approve once"],
           context={"kind": "gate", "gate_type": "tool", "tool_name": "x",
                    "verdict": "require_approval", "execution_id": "exec_o"},
           dedup_key="tool:open")
    exp_id = q.post(title="t", body="b", options=["Deny", "Approve once"],
                    context={"kind": "gate", "gate_type": "tool", "tool_name": "y",
                             "verdict": "require_approval", "execution_id": "exec_e"},
                    dedup_key="tool:exp")
    q.expire_by_dedup_key("tool:exp")
    rows = [r for r in ledger.project(v, data_dir=tmp_path) if r.event_kind == "gate_decision"]
    assert [r.source_id for r in rows] == [exp_id]
    c = _cells(rows[0])
    assert c["outcome_status"] == "expired"
    assert c["gate_resolved_via"] == ledger.NOT_APPLICABLE


def test_command_card_exports_no_command_text(tmp_path):
    """A command card stores the raw command; the row exports none of it, names the
    tool and the effect class as not recorded, and keeps its verdict."""
    v = _vault(tmp_path)
    q = OperatorDecisionQueue(v)
    secret_cmd = "curl -H 'Authorization: Bearer " + "z" * 300 + "' https://x"
    did = q.post(title=f"Run `{secret_cmd}`?", body=secret_cmd,
                 options=["Deny", "Approve once", "Always allow"],
                 context={"kind": "gate", "gate_type": "command", "command": secret_cmd,
                          "cwd": "C:/Users/someone", "verdict": "require_approval",
                          "effect_tags": [], "destructive": True,
                          "inspect": secret_cmd, "execution_id": "exec_cmd"},
                 dedup_key="command:abc")
    q.resolve(did, choice="Deny")
    rows = ledger.project(v, data_dir=tmp_path)
    row = [r for r in rows if r.event_kind == "gate_decision"][0]
    c = _cells(row)
    assert c["gate_verdict"] == "require_approval" and c["outcome_status"] == "Deny"
    assert c["action_tool"] == ledger.NOT_RECORDED
    assert c["action_effect_tags"] == ledger.NOT_RECORDED
    assert "command text" in row.action["not_recorded_because"]["effect_tags"]
    for blob in (ledger.export_csv(rows), ledger.export_jsonl(rows)):
        assert b"zzzzzzzzzz" not in blob and b"curl" not in blob
        assert b"someone" not in blob


def test_a_free_text_answer_is_never_exported(tmp_path):
    v = _vault(tmp_path)
    q = OperatorDecisionQueue(v)
    answer = "my password is hunter2-" + "x" * 300
    did = q.post(title="t", body="b", options=["Submit"],
                 context={"kind": "gate", "gate_type": "harness", "harness_kind": "input",
                          "execution_id": "exec_in"},
                 dedup_key="harness:in")
    q.resolve(did, choice=answer)
    rows = ledger.project(v, data_dir=tmp_path)
    c = _cells([r for r in rows if r.event_kind == "gate_decision"][0])
    assert c["outcome_status"].startswith("answered (free text")
    assert c["gate_verdict"] == ledger.NOT_RECORDED
    for blob in (ledger.export_csv(rows), ledger.export_jsonl(rows)):
        assert b"hunter2" not in blob


def test_policy_auto_grant_row_resolves_via_auto_policy(tmp_path):
    from systemu.interface.command.gate import GateDescriptor
    from systemu.interface.command.inbox import InboxQueue
    v = _vault(tmp_path)
    desc = GateDescriptor.from_command(tool_name="run_command", command="dir", cwd="")
    InboxQueue(v)._save_auto_allow_audit_row(desc, "command",
                                             {"kind": "gate", "gate_type": "command",
                                              "execution_id": "exec_pol"}, vault=v)
    row = [r for r in ledger.project(v, data_dir=tmp_path)
           if r.event_kind == "gate_decision"][0]
    assert _cells(row)["gate_resolved_via"] == "auto:policy"


def test_decision_rows_follow_the_window_and_the_execution_filter(tmp_path):
    v = _vault(tmp_path)
    q = OperatorDecisionQueue(v)
    for eid in ("exec_w1", "exec_w2"):
        did = q.post(title="t", body="b", options=["Deny", "Approve once"],
                     context={"kind": "gate", "gate_type": "tool", "tool_name": "x",
                              "verdict": "require_approval", "execution_id": eid},
                     dedup_key=f"tool:{eid}")
        q.resolve(did, choice="Approve once")
    _act(v, "exec_w1")
    only = ledger.project(v, data_dir=tmp_path, execution_id="exec_w1")
    assert {r.actor["run_ref"]["execution_id"] for r in only
            if r.event_kind == "gate_decision"} == {"exec_w1"}
    assert [r for r in ledger.project(v, data_dir=tmp_path, until_ts="2000-01-01T00:00:00Z")
            if r.event_kind == "gate_decision"] == []


# ═════════════════════════════════════════════════════════════════════════════
#  3. the export surface
# ═════════════════════════════════════════════════════════════════════════════

def test_gate_columns_leave_the_never_populated_list_and_row_kinds_name_decisions():
    for col in ("gate_verdict", "gate_decision_id", "gate_resolved_via"):
        assert col not in ledger_export.UNPOPULATED_COLUMNS
    assert set(ledger_export.ROW_KINDS) == {"effect", "committed_effect", "gate_decision"}
    for text in ledger_export.ROW_KINDS.values():
        text.encode("ascii")


# ── the proposed rewrites of the two existing pins this change turns red ─────
#
# tests/test_rp3b_compliance_export.py::test_manifest_names_every_unpopulated_column_with_a_reason
#   old: assert "does NOT mean no approval was required" in never["gate_verdict"]
#   new: the assertions below (gate_verdict is no longer never-populated; the guard
#        against the misreading moves into the cell itself).
# tests/test_rp3b_compliance_export.py::test_preview_names_destination_row_count_and_the_blank_columns
#   old: assert "gate_verdict" in joined                        # named, not just counted
#   new: assert "criteria_met" in joined                        # named, not just counted

def test_proposed_rewrite_manifest_reason_pin(tmp_path):
    from systemu.storage.file_vault import FileVault
    from systemu.vault.vault import Vault
    fv = FileVault(Vault(str(tmp_path / "vault")))
    audit_log.append_action(fv._v, execution_id="quick_1", objective_id=0,
                            action="send_email", params={"to": "p@example.com"},
                            success=True)
    res = ledger_export.write_export(fv, fmt="csv", data_dir=tmp_path)
    m = json.loads(Path(res["manifest_path"]).read_text(encoding="utf-8"))
    never = m["columns_never_populated_by_this_build"]
    assert set(never) == set(ledger_export.UNPOPULATED_COLUMNS)
    assert all(isinstance(why, str) and why for why in never.values())
    assert "gate_verdict" not in never
    text = Path(res["destination_path"]).read_bytes().decode("utf-8")
    header, first = text.splitlines()[0].split(","), text.splitlines()[1].split(",")
    assert dict(zip(header, first))["gate_verdict"] == ledger.NOT_RECORDED
    assert "not_recorded" in m["reading_note"]


def test_proposed_rewrite_preview_pin(tmp_path):
    from systemu.storage.file_vault import FileVault
    from systemu.vault.vault import Vault
    fv = FileVault(Vault(str(tmp_path / "vault")))
    audit_log.append_action(fv._v, execution_id="quick_1", objective_id=0,
                            action="send_email", params={"to": "p@example.com"},
                            success=True)
    pv = ledger_export.preview(fv, fmt="csv", data_dir=tmp_path)
    from systemu.interface.pages.settings import export_preview_lines
    joined = "\n".join(export_preview_lines(pv))
    assert "ALWAYS blank in this build" in joined
    assert "criteria_met" in joined
