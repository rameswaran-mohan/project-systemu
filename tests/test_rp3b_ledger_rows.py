"""R-P3b -- the action-ledger rows carry what the gate decided and what was committed.

Before this slice every effect row exported ``action_effect_tags`` as an empty cell
(``ledger._effect_row`` hard-coded ``[]``) and no committed-effect row existed at all.
The data was never lost upstream; it was never RECORDED: the sandbox gate scored each
call and threw the outcome away, and the durable receipt could not say whether it was
a live verification or the S4 shadow meter's record-only measurement.

What these pins hold, end to end:

  * the gate RECORDS how each call got past it (``ToolResult.gate``), from values it
    already held, on every pass path;
  * both workflow-lane audit writers STAMP that record on the row they write;
  * the projection reads the stamp -- never re-derives tags from the tool as it is
    today -- and names an absent value ``not_recorded`` with a reason (DEC-27);
  * ``committed_effect`` rows come only from receipts that WITNESS a live, confirmed
    effect (``shadow is False``), scoped to this vault's runs;
  * nothing secret reaches either export (MASK before anything, DEC-31).

Every "remove the production call site" mutation of this slice turns a NAMED test here
red; the list is in the slice report.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.approval.decision_queue import OperatorDecision, OperatorDecisionQueue
from systemu.approval.exceptions import PendingOperatorDecision
from systemu.core.models import Tool, ToolType
from systemu.runtime import audit_log, ledger, ledger_export, receipts_store
from systemu.runtime.command_approvals import CommandApprovalStore
from systemu.runtime.tool_sandbox import ToolResult, ToolSandbox


# ─────────────────────────────────────────────────────────────────────────────
#  helpers
# ─────────────────────────────────────────────────────────────────────────────

class _MemVault:
    """The REAL decision queue on an in-memory vault (only the three methods
    OperatorDecisionQueue uses), so every card is posted / resolved / consumed by
    production code."""

    def __init__(self, root=None):
        self.root = root
        self._decisions = {}

    def load_index(self, name):
        if name != "decisions":
            return []
        return [d.to_dict() for d in self._decisions.values()]

    def get_decision(self, did):
        return self._decisions[did]

    def save_decision(self, decision):
        self._decisions[decision.id] = OperatorDecision.from_dict(decision.to_dict())


def _impl(tmp_path, name, body_extra=""):
    """A real implementation file under THIS test's tmp_path (never a shared dir:
    identical-size bodies in a shared dir let __pycache__ serve another test's
    bytecode)."""
    impl_dir = tmp_path / "impls"
    impl_dir.mkdir(parents=True, exist_ok=True)
    impl = impl_dir / f"{name}.py"
    impl.write_text(
        "def run(target=None, command=None, cwd=None):\n"
        f"    # {body_extra}\n"
        "    return {'success': True}\n", encoding="utf-8")
    return impl


def _tool(tmp_path, name, tags):
    impl = _impl(tmp_path, name, body_extra=name)
    return Tool(id=f"tool_{name}", name=name, description="d",
                tool_type=ToolType.PYTHON_FUNCTION,
                implementation_path=str(impl.relative_to(tmp_path)),
                effect_tags=list(tags), version=1)


def _sandbox(tmp_path, vault):
    root = tmp_path / "vault"
    root.mkdir(parents=True, exist_ok=True)
    store = CommandApprovalStore(tmp_path / "command_approvals.json")
    return ToolSandbox(str(root), vault=vault, command_approvals=store), store


def _run(sb, tool, params, **kw):
    return asyncio.run(sb.execute_tool(tool.implementation_path, params,
                                       tool=tool, **kw))


def _file_vault(tmp_path):
    """The shape ledger.project reads in the full projection: a file vault root."""
    root = tmp_path / "ledger_vault"
    (root / "audit").mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(root=root)


def _write_audit(vault, entries):
    p = Path(vault.root) / "audit" / "actions.jsonl"
    with p.open("a", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def _audit(eid, oid, action, *, ts="2026-10-01T10:00:00Z", gate=None, params=None):
    e = {"ts": ts, "execution_id": eid, "objective_id": oid, "action": action,
         "params": params if params is not None else {"target": "x"},
         "success": True, "error": None}
    if gate is not None:
        e["gate"] = gate
    return e


def _cells(row):
    return dict(zip(ledger._COLUMNS, ledger._csv_cells(row)))


_SCORED = {"gate": "tool", "verdict": "require_approval", "passed_via": "approve_once",
           "decision_id": "dec_abc12345", "effect_tags": ["net_mutate", "send_message"],
           "effect_tags_basis": "scored"}


# ═════════════════════════════════════════════════════════════════════════════
#  1. THE CHOKEPOINT: the gate records how each call got past it
# ═════════════════════════════════════════════════════════════════════════════

def test_allow_band_pass_records_verdict_and_scored_tags(tmp_path):
    vault = _MemVault(root=tmp_path)
    sb, _store = _sandbox(tmp_path, vault)
    tool = _tool(tmp_path, "read_notes", ["local_read"])
    res = _run(sb, tool, {"target": "notes"})
    assert res.success, res.error
    assert res.gate == {"gate": "tool", "verdict": "allow", "passed_via": "auto_allow",
                        "decision_id": None, "effect_tags": ["local_read"],
                        "effect_tags_basis": "scored"}
    # provenance only -- never shown to the model
    assert "gate" not in res.to_dict()


def test_approve_once_pass_records_the_consumed_decision_id(tmp_path):
    """REQUIRE_APPROVAL card -> operator 'Approve once' -> the chat lane re-calls with
    the resolved dedup: the record names THAT decision, and the governor's EFFECTIVE
    tags (an untagged tool scores 'unknown'), never the empty declared set."""
    vault = _MemVault(root=tmp_path)
    sb, _store = _sandbox(tmp_path, vault)
    tool = _tool(tmp_path, "consolidate_records", [])
    with pytest.raises(PendingOperatorDecision) as exc:
        _run(sb, tool, {"target": "q3"})
    card = OperatorDecisionQueue(vault).list_pending()[0]
    OperatorDecisionQueue(vault).resolve(card.id, choice="Approve once")
    res = _run(sb, tool, {"target": "q3"}, _command_gate_resolved=exc.value.dedup_key)
    assert res.success, res.error
    assert res.gate["gate"] == "tool"
    assert res.gate["verdict"] == "require_approval"
    assert res.gate["passed_via"] == "approve_once"
    assert res.gate["decision_id"] == card.id
    assert res.gate["effect_tags"] == ["unknown"]
    assert res.gate["effect_tags_basis"] == "scored"


def test_standing_approval_pass_records_its_path_without_a_decision_id(tmp_path):
    vault = _MemVault(root=tmp_path)
    sb, store = _sandbox(tmp_path, vault)
    tool = _tool(tmp_path, "sync_records", [])
    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, {"target": "q3"})
    card = OperatorDecisionQueue(vault).list_pending()[0]
    store.approve(card.context["tool_signature"])
    res = _run(sb, tool, {"target": "q3"})
    assert res.success, res.error
    assert res.gate["passed_via"] == "standing_approval"
    assert res.gate["verdict"] == "require_approval"
    assert res.gate["decision_id"] is None          # the allow-list keeps no decision id


def test_command_gate_records_a_read_only_command_without_inventing_a_class(tmp_path):
    """No Tool in scope: only the command gate looked, and it scores the command TEXT,
    so the record carries no effect class rather than an empty 'no effects' one."""
    vault = _MemVault(root=tmp_path)
    sb, _store = _sandbox(tmp_path, vault)
    impl = _impl(tmp_path, "run_command", body_extra="shell")
    res = asyncio.run(sb.execute_tool(str(impl.relative_to(tmp_path)),
                                      {"command": "ver"}))
    assert res.success, res.error
    assert res.gate == {"gate": "command", "verdict": "allow",
                        "passed_via": "read_only_command", "decision_id": None,
                        "effect_tags": None, "effect_tags_basis": None}


def test_resume_bridge_pass_records_its_path(tmp_path):
    vault = _MemVault(root=tmp_path)
    sb, store = _sandbox(tmp_path, vault)
    tool = _tool(tmp_path, "merge_ledgers", [])
    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, {"target": "q3"})
    card = OperatorDecisionQueue(vault).list_pending()[0]
    store.mark_resume_approved(card.context["tool_signature"])
    res = _run(sb, tool, {"target": "q3"})
    assert res.success, res.error
    assert (res.gate["passed_via"], res.gate["decision_id"]) == ("resume_bridge", None)


def test_registry_fast_path_carries_the_record_too(tmp_path):
    """The in-process registry path (built-ins, operator-trusted tools) returns its
    own ToolResult; it must carry the record like the subprocess path does."""
    class _Registry:
        async def execute(self, name, params, timeout=None):
            return {"success": True, "ran": name}

    vault = _MemVault(root=tmp_path)
    sb, _store = _sandbox(tmp_path, vault)
    sb.attach_registry(_Registry())
    tool = _tool(tmp_path, "read_notes", ["local_read"])
    res = _run(sb, tool, {"target": "notes"})
    assert res.success and res.parsed.get("ran") == "read_notes", res
    assert res.gate["passed_via"] == "auto_allow"


@pytest.mark.parametrize("how", ["standing", "bridge", "approve_once"])
def test_command_gate_records_each_approved_pass(tmp_path, how):
    from systemu.runtime.command_approvals import command_signature
    vault = _MemVault(root=tmp_path)
    sb, store = _sandbox(tmp_path, vault)
    impl = _impl(tmp_path, "run_command", body_extra="shell")
    rel = str(impl.relative_to(tmp_path))
    params = {"command": "del report.tmp"}
    with pytest.raises(PendingOperatorDecision) as exc:
        asyncio.run(sb.execute_tool(rel, params))
    sig = command_signature("del report.tmp", cwd="")
    kw = {}
    card_id = None                      # a bridge minted here carries no decision id
    if how == "standing":
        store.approve(sig, command="del report.tmp")
    elif how == "bridge":
        store.mark_resume_approved(sig)
    else:
        card = OperatorDecisionQueue(vault).list_pending()[0]
        OperatorDecisionQueue(vault).resolve(card.id, choice="Approve once")
        kw["_command_gate_resolved"] = exc.value.dedup_key
        card_id = card.id               # round 2 (e): the consumed decision is named
    res = asyncio.run(sb.execute_tool(rel, params, **kw))
    assert res.success, res.error
    want = {"standing": "standing_approval", "bridge": "resume_bridge",
            "approve_once": "approve_once"}[how]
    assert res.gate == {"gate": "command", "verdict": "require_approval",
                        "passed_via": want, "decision_id": card_id,
                        "effect_tags": None, "effect_tags_basis": None}


def test_no_tool_and_no_command_gate_records_nothing(tmp_path):
    vault = _MemVault(root=tmp_path)
    sb, _store = _sandbox(tmp_path, vault)
    impl = _impl(tmp_path, "legacy_thing")
    res = asyncio.run(sb.execute_tool(str(impl.relative_to(tmp_path)), {"target": "x"}))
    assert res.success, res.error
    assert res.gate is None


# ═════════════════════════════════════════════════════════════════════════════
#  2. THE WRITERS: both workflow-lane audit writers stamp the record
# ═════════════════════════════════════════════════════════════════════════════

def test_action_tool_audit_hook_stamps_the_gate_record(tmp_path):
    from systemu.vault.vault import Vault
    from sharing_on.config import Config
    v = Vault(root=tmp_path)
    tool = Tool(id="t1", name="email_send", description="d",
                tool_type=ToolType.PYTHON_FUNCTION, is_action_tool=True)
    sb = ToolSandbox(vault_root=tmp_path, vault=v, config=Config())
    sb._after_successful_call(tool=tool, params={"to": "a@b.example"},
                              execution_id="exec_h", objective_id=1, gate=dict(_SCORED))
    rows = v.query_action_audit(execution_id="exec_h")
    assert len(rows) == 1 and rows[0]["gate"] == _SCORED


def test_append_action_without_a_gate_writes_no_gate_key(tmp_path):
    from systemu.vault.vault import Vault
    v = Vault(root=tmp_path)
    audit_log.append_action(v, execution_id="e", objective_id=0, action="a",
                            params={}, success=True)
    assert "gate" not in v.query_action_audit(execution_id="e")[0]


def test_keystone_entry_builder_stamps_only_a_dict(tmp_path):
    from systemu.runtime.shadow_runtime import _build_tool_audit_entry
    e = _build_tool_audit_entry(execution_id="x", objective_id=1, tool_name="t",
                                params={}, gate=dict(_SCORED))
    assert e["gate"] == _SCORED
    e2 = _build_tool_audit_entry(execution_id="x", objective_id=1, tool_name="t",
                                 params={}, gate="not-a-record")
    assert "gate" not in e2


def test_handle_tool_call_hands_the_result_gate_to_the_action_tool_hook(tmp_path,
                                                                         monkeypatch):
    """The REAL _handle_tool_call success path: the ToolResult the sandbox returns
    carries the record, and the action-tool hook writes it on the durable row."""
    from systemu.runtime.shadow_runtime import ShadowRuntime
    from systemu.runtime.context_builder import ExecutionContext
    from systemu.vault.vault import Vault

    class _Cfg:
        def __init__(self, out):
            self.output_dir = out
            self.check_fn_cache_ttl_seconds = 30
            self.capability_track_outcomes = False

    monkeypatch.chdir(tmp_path)                     # metrics side stores write to ./data
    v = Vault(root=tmp_path / "v")
    rt = ShadowRuntime.__new__(ShadowRuntime)
    out = tmp_path / "output"
    out.mkdir(parents=True, exist_ok=True)
    rt.config = _Cfg(str(out))
    rt.vault = v
    rt.sandbox = ToolSandbox(vault_root=tmp_path, backend="local", vault=v,
                             config=rt.config)
    rt._dep_failed_tools, rt._consec_tool_fails = {}, {}
    rt._fresh_work_since_last_verifier_call = False
    rt._execution_mind = None

    tool = Tool(id="t_post", name="post_status", description="d",
                tool_type=ToolType.PYTHON_FUNCTION, is_action_tool=True,
                implementation_path="tools/implementations/post_status.py")

    async def _fake_execute_tool(impl_path, params, **kw):
        return ToolResult(success=True, parsed={"success": True}, gate=dict(_SCORED))
    monkeypatch.setattr(rt.sandbox, "execute_tool", _fake_execute_tool)

    ctx = ExecutionContext(execution_id="exec_htc", system_prompt="",
                           scroll_json=[], tool_index=[])
    decision = {"decision": "TOOL_CALL", "tool_name": "post_status",
                "parameters": {"target": "x"}, "reasoning": "r",
                "completes_objective": 2}
    result = asyncio.run(rt._handle_tool_call(decision, tools=[tool], context=ctx,
                                              current_ab=0, dry_run=False,
                                              execution_id="exec_htc"))
    assert result is not None and result.success
    rows = v.query_action_audit(execution_id="exec_htc")
    assert [r.get("gate") for r in rows] == [_SCORED]


def test_keystone_writes_the_gate_record_through_the_real_loop(tmp_path, monkeypatch):
    """ShadowRuntime.execute's tool-success auto-audit (the row written for EVERY
    successful call) carries the record of the call's ToolResult."""
    from unittest.mock import patch
    from sharing_on.config import Config
    from systemu.core.models import Objective
    from systemu.runtime.shadow_runtime import ShadowRuntime
    import systemu.runtime.shadow_runtime as _sr
    from test_s3_credit_wiring import (_build_entities_objs, _redirect_snapshot_io,
                                       _softpass_outcome)

    monkeypatch.chdir(tmp_path)
    vault, shadow, activity = _build_entities_objs(
        tmp_path, [Objective(id=1, goal="do it", success_criteria="done")])
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    _redirect_snapshot_io(monkeypatch, tmp_path / "snap_data")
    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request", lambda *a, **k: "card",
                        raising=False)
    runtime = ShadowRuntime(cfg, vault)

    async def _handle(decision, tools, context, current_ab, dry_run, **kw):
        return ToolResult(success=True, parsed={"ok": True}, gate=dict(_SCORED))
    monkeypatch.setattr(runtime, "_handle_tool_call", _handle)
    monkeypatch.setattr(_sr, "process_completion_claim", _softpass_outcome)

    decisions = [
        {"action": "TOOL_CALL", "tool_name": "api_tool", "parameters": {"target": "x"},
         "completes_objective": 1, "reasoning": "do"},
        {"action": "FAIL", "reason": "only if not credited"},
    ]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        asyncio.run(runtime.execute(shadow, activity))

    lines = (tmp_path / "audit" / "actions.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(ln) for ln in lines if ln.strip()]
    tool_rows = [r for r in rows if r.get("action") == "api_tool"]
    assert tool_rows, rows
    assert all(r.get("gate") == _SCORED for r in tool_rows), tool_rows


# ═════════════════════════════════════════════════════════════════════════════
#  3. THE PROJECTION: effect rows read the stamp; an absent value is NAMED
# ═════════════════════════════════════════════════════════════════════════════

def test_stamped_effect_row_carries_the_recorded_classes(tmp_path):
    v = _file_vault(tmp_path)
    _write_audit(v, [_audit("exec_1", 0, "send_update", gate=dict(_SCORED))])
    row = ledger.project(v, data_dir=tmp_path)[0]
    assert row.action["effect_tags"] == ["net_mutate", "send_message"]
    assert row.action["effect_tags_basis"] == "scored"
    assert "not_recorded_because" not in row.action
    assert _cells(row)["action_effect_tags"] == "net_mutate|send_message"


def test_unstamped_effect_row_says_not_recorded_never_blank(tmp_path):
    """DEC-27: an entry with no gate stamp (written before this build, or by a writer
    with no gate outcome) must not export a blank / [] that reads as 'no effects'."""
    v = _file_vault(tmp_path)
    _write_audit(v, [_audit("exec_2", 0, "legacy_action")])
    row = ledger.project(v, data_dir=tmp_path)[0]
    assert row.action["effect_tags"] is None
    assert "no gate stamp" in row.action["not_recorded_because"]["effect_tags"]
    assert _cells(row)["action_effect_tags"] == ledger.NOT_RECORDED
    line = json.loads(ledger.export_jsonl([row]).decode("utf-8"))
    assert line["action"]["effect_tags"] is None
    assert "effect_tags" in line["action"]["not_recorded_because"]


def test_command_gate_stamp_without_a_class_names_why(tmp_path):
    v = _file_vault(tmp_path)
    stamp = {"gate": "command", "verdict": "allow", "passed_via": "read_only_command",
             "decision_id": None, "effect_tags": None, "effect_tags_basis": None}
    _write_audit(v, [_audit("exec_3", 0, "run_command", gate=stamp)])
    row = ledger.project(v, data_dir=tmp_path)[0]
    assert _cells(row)["action_effect_tags"] == ledger.NOT_RECORDED
    assert "command text" in row.action["not_recorded_because"]["effect_tags"]


def test_malformed_stamp_values_are_not_believed(tmp_path):
    """The stamp is data off disk: a non-list, an empty list or a non-str member is
    not a recorded class set."""
    v = _file_vault(tmp_path)
    for i, bad in enumerate(([], "net_mutate", [1, "x"], [""], None)):
        _write_audit(v, [_audit(f"exec_m{i}", 0, "a", ts=f"2026-10-01T10:00:0{i}Z",
                                gate={"gate": "tool", "effect_tags": bad})])
    _write_audit(v, [_audit("exec_m9", 0, "a", ts="2026-10-01T10:00:09Z",
                            gate=["not", "a", "dict"])])
    for row in ledger.project(v, data_dir=tmp_path):
        assert _cells(row)["action_effect_tags"] == ledger.NOT_RECORDED, row.action


def test_recorded_tags_without_a_basis_name_the_missing_basis(tmp_path):
    v = _file_vault(tmp_path)
    _write_audit(v, [_audit("exec_b", 0, "a",
                            gate={"gate": "tool", "effect_tags": ["local_write"]})])
    row = ledger.project(v, data_dir=tmp_path)[0]
    assert row.action["effect_tags"] == ["local_write"]
    assert row.action["effect_tags_basis"] is None
    assert "effect_tags_basis" in row.action["not_recorded_because"]


def test_end_to_end_gate_to_export_cell(tmp_path):
    """Chokepoint -> writer -> projection -> CSV cell, with no hand-built stamp: the
    real gate scores the call, the real hook writes the row, the real exporter
    renders it."""
    from systemu.vault.vault import Vault
    from sharing_on.config import Config
    vroot = tmp_path / "e2e_vault"
    v = Vault(root=vroot)
    sb = ToolSandbox(str(tmp_path / "vault"), vault=v, config=Config(),
                     command_approvals=CommandApprovalStore(tmp_path / "ca.json"))
    tool = _tool(tmp_path, "read_notes", ["local_read"])
    tool.is_action_tool = True
    res = _run(sb, tool, {"target": "notes"})
    assert res.success, res.error
    sb._after_successful_call(tool=tool, params={"target": "notes"},
                              execution_id="exec_e2e", objective_id=0,
                              gate=getattr(res, "gate", None))
    rows = ledger.project(SimpleNamespace(root=vroot), data_dir=tmp_path)
    assert [_cells(r)["action_effect_tags"] for r in rows] == ["local_read"]
    assert rows[0].action["effect_tags_basis"] == "scored"


# ═════════════════════════════════════════════════════════════════════════════
#  4. COMMITTED EFFECTS: only a WITNESSED live, confirmed receipt makes a row
# ═════════════════════════════════════════════════════════════════════════════

def _receipt(data_dir, eid, oid, **fields):
    base = {"objective_id": oid, "confirmed": True, "method": "api_readback",
            "detail": "issue #412 created", "stamped_at": "2026-10-01T10:00:05Z",
            "shadow": False}
    base.update(fields)
    receipts_store.write_receipt(eid, oid, base, data_dir=data_dir)


def test_live_confirmed_receipt_projects_one_committed_effect_row(tmp_path):
    v = _file_vault(tmp_path)
    _write_audit(v, [_audit("exec_c1", 4, "create_issue", gate=dict(_SCORED))])
    _receipt(tmp_path, "exec_c1", 4)
    rows = ledger.project(v, data_dir=tmp_path)
    kinds = [(r.event_kind, r.source_kind) for r in rows]
    assert kinds == [("effect", "action_audit"),
                     ("committed_effect", "committed_effect")]
    effect, committed = rows
    assert committed.ts == "2026-10-01T10:00:05.000000Z"
    assert committed.source_id == "exec_c1#4"
    assert committed.outcome["status"] == "committed"
    assert committed.outcome["verification"] == "verified"
    # the witnessed link to the effect row: same evidence ref AND fingerprint
    assert committed.outcome["evidence_ref"] == effect.outcome["evidence_ref"]
    assert committed.outcome["evidence_fingerprint"] == effect.outcome["evidence_fingerprint"]
    cells = _cells(committed)
    assert cells["action_tool"] == ledger.NOT_RECORDED
    assert cells["action_effect_tags"] == ledger.NOT_RECORDED
    assert cells["action_params_digest"] == ledger.NOT_RECORDED
    assert cells["event_kind"] == "committed_effect"


@pytest.mark.parametrize("fields", [
    {"shadow": True},                       # the S4 meter's record-only measurement
    {"shadow": None},                       # not a bool
    {"confirmed": False},                   # claimed, not verified (DEC-13)
    {"confirmed": "yes"},                   # truthy is not True
    {"detail": "   "},                      # IMPL-7: blank detail is no effect
    {"stamped_at": None},                   # no verification time
    {"stamped_at": "yesterday"},            # unparseable
])
def test_receipt_that_does_not_witness_a_live_confirmed_effect_yields_no_row(
        tmp_path, fields):
    v = _file_vault(tmp_path)
    _write_audit(v, [_audit("exec_c2", 1, "create_issue")])
    _receipt(tmp_path, "exec_c2", 1, **fields)
    assert [r.event_kind for r in ledger.project(v, data_dir=tmp_path)] == ["effect"]


def test_receipt_written_before_the_live_flag_existed_yields_no_row(tmp_path):
    v = _file_vault(tmp_path)
    _write_audit(v, [_audit("exec_c3", 1, "create_issue")])
    target = tmp_path / "audit" / "exec_exec_c3" / "receipts.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"1": {
        "objective_id": 1, "confirmed": True, "method": "api_readback",
        "detail": "done", "stamped_at": "2026-10-01T10:00:05Z"}}), encoding="utf-8")
    rows = ledger.project(v, data_dir=tmp_path)
    assert [r.event_kind for r in rows] == ["effect"]
    assert rows[0].outcome["verification"] == "verified"     # the existing join stands


def test_receipts_of_runs_outside_this_vaults_audit_are_not_projected(tmp_path):
    v = _file_vault(tmp_path)
    _write_audit(v, [_audit("exec_mine", 1, "create_issue")])
    _receipt(tmp_path, "exec_someone_else", 1)
    assert [r.source_id for r in ledger.project(v, data_dir=tmp_path)
            if r.event_kind == "committed_effect"] == []


def test_committed_effect_is_windowed_on_its_own_stamp(tmp_path):
    """The tool call landed before the window; its verification was stamped inside
    it. The committed effect belongs to the window, the call does not."""
    v = _file_vault(tmp_path)
    _write_audit(v, [_audit("exec_w", 1, "create_issue", ts="2026-09-30T23:59:59Z")])
    _receipt(tmp_path, "exec_w", 1, stamped_at="2026-10-01T00:00:01Z")
    rows = ledger.project(v, data_dir=tmp_path, since_ts="2026-10-01T00:00:00Z",
                          until_ts="2026-10-01T23:59:59Z")
    assert [(r.event_kind, r.source_id) for r in rows] == [
        ("committed_effect", "exec_w#1")]
    late = ledger.project(v, data_dir=tmp_path, since_ts="2026-10-02T00:00:00Z")
    assert late == []


def test_a_secret_in_the_receipt_detail_never_reaches_either_export(tmp_path):
    """DEC-31: the detail is MASKed before it is placed anywhere, and it is never
    exported at all. The secret is longer than every cap on this path (there is no
    truncation on it), so a clip could not be what hides it."""
    secret = "ghp_" + "Q7" * 150                     # 304 chars
    v = _file_vault(tmp_path)
    _write_audit(v, [_audit("exec_s", 1, "create_issue")])
    _receipt(tmp_path, "exec_s", 1, detail=f"created with token {secret}")
    rows = ledger.project(v, data_dir=tmp_path)
    committed = [r for r in rows if r.event_kind == "committed_effect"][0]
    assert secret not in json.dumps(committed.raw_beside)
    assert "created with token" in committed.raw_beside["detail"]
    for blob in (ledger.export_csv(rows), ledger.export_jsonl(rows)):
        assert secret.encode() not in blob
        assert b"created with token" not in blob


def test_live_persist_hook_writes_a_receipt_the_ledger_projects(tmp_path, monkeypatch):
    """The REAL single receipt writer: a live verification is marked shadow=False and
    becomes a committed effect; the meter's record_only call is marked shadow=True and
    does not."""
    from systemu.core.models import ExternalEvidence
    from systemu.runtime import shadow_runtime as sr
    from systemu.runtime.chat_submission_ctx import set_execution_id

    monkeypatch.chdir(tmp_path)                     # receipts default to ./data
    v = _file_vault(tmp_path)
    _write_audit(v, [_audit("exec_live", 1, "create_issue"),
                     _audit("exec_meter", 1, "create_issue", ts="2026-10-01T10:00:01Z")])
    ev = dict(objective_id=1, confirmed=True, method="api_readback",
              detail="issue #9 created", stamped_at="2026-10-01T10:00:05Z")
    try:
        set_execution_id("exec_live")
        sr._persist_external_evidence(type("C", (), {})(), ExternalEvidence(**ev))
        set_execution_id("exec_meter")
        sr._persist_external_evidence(type("C", (), {})(), ExternalEvidence(**ev),
                                      record_only=True)
    finally:
        set_execution_id(None)
    assert receipts_store.read_receipts("exec_live")["1"]["shadow"] is False
    assert receipts_store.read_receipts("exec_meter")["1"]["shadow"] is True
    committed = [r.source_id for r in ledger.project(v)
                 if r.event_kind == "committed_effect"]
    assert committed == ["exec_live#1"]


def test_shadow_meter_receipt_is_marked_record_only_through_the_real_loop(
        tmp_path, monkeypatch):
    """S4 SHADOW run, real loop: the meter's confirmed measurement must reach the
    durable receipt as shadow=True, so it never becomes a committed effect."""
    from test_ra13b1_shadow_meter import _shadow_obj, _stamp_shadow_on_resolve
    from test_s3_credit_wiring import _CreateOnceReadbackClient, _drive_live_credit

    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "shadow")
    _stamp_shadow_on_resolve(monkeypatch)
    token, url = "sub-ledger-meter", "https://api.example.com/rows/5"
    client = _CreateOnceReadbackClient(echo_tokens=[token])
    directive = {"readback_url": url, "expected_tokens": [token],
                 "submit_host": "api.example.com"}
    tool_parsed = {"ok": True, "external": {
        "strategy": "api_readback", "expected_tokens": [token],
        "readback_url": url, "submit_host": "api.example.com"}}
    runtime, _result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[_shadow_obj()], claim_obj_id=1,
        tool_parsed=tool_parsed, api_client=client,
        decision_params={"external": directive})
    ev = (getattr(ctx, "_external_evidence", {}) or {}).get("1")
    assert ev and ev.get("shadow") is True and ev.get("confirmed") is True, ev

    receipts = list((cwd / "data" / "audit").glob("exec_*/receipts.json"))
    assert receipts, "the meter must still write its durable receipt"
    for p in receipts:
        for r in json.loads(p.read_text(encoding="utf-8")).values():
            assert r.get("shadow") is True, r
    rows = ledger.project(SimpleNamespace(root=Path(runtime.vault.root)))
    assert [r for r in rows if r.event_kind == "committed_effect"] == []
    assert [r for r in rows if r.event_kind == "effect"]


def test_live_verification_through_the_real_loop_becomes_a_committed_effect(
        tmp_path, monkeypatch):
    from test_s3_credit_wiring import (_CreateOnceReadbackClient, _drive_live_credit,
                                       _external_obj)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    token, url = "sub-ledger-live", "https://api.example.com/rows/6"
    client = _CreateOnceReadbackClient(echo_tokens=[token])
    directive = {"readback_url": url, "expected_tokens": [token],
                 "submit_host": "api.example.com"}
    tool_parsed = {"ok": True, "external": {
        "strategy": "api_readback", "expected_tokens": [token],
        "submission_token": token, "readback_url": url,
        "submit_host": "api.example.com"}}
    runtime, result, _ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[_external_obj()], claim_obj_id=1,
        tool_parsed=tool_parsed, api_client=client,
        decision_params={"external": directive})
    assert result.get("status") == "success", result
    rows = ledger.project(SimpleNamespace(root=Path(runtime.vault.root)))
    committed = [r for r in rows if r.event_kind == "committed_effect"]
    assert len(committed) == 1, [(r.event_kind, r.source_id) for r in rows]
    effect = [r for r in rows if r.event_kind == "effect"
              and r.outcome.get("evidence_ref") == committed[0].outcome["evidence_ref"]]
    assert effect and (effect[0].outcome["evidence_fingerprint"]
                       == committed[0].outcome["evidence_fingerprint"])


# ═════════════════════════════════════════════════════════════════════════════
#  5. THE SURFACE: the compliance export carries the new rows and says what they are
# ═════════════════════════════════════════════════════════════════════════════

def test_export_carries_effect_tags_and_committed_rows_with_stable_columns(tmp_path):
    from systemu.storage.file_vault import FileVault
    from systemu.vault.vault import Vault
    fv = FileVault(Vault(str(tmp_path / "vault")))
    audit_log.append_action(fv._v, execution_id="exec_x", objective_id=2,
                            action="create_issue", params={"title": "t"}, success=True,
                            gate=dict(_SCORED))
    from datetime import datetime, timezone
    _receipt(tmp_path, "exec_x", 2,
             stamped_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    res = ledger_export.write_export(fv, fmt="csv", data_dir=tmp_path)
    text = Path(res["destination_path"]).read_bytes().decode("utf-8")
    reader = list(csv.reader(io.StringIO(text)))
    assert reader[0] == list(ledger._COLUMNS)                # names unchanged, 25
    body = [dict(zip(reader[0], r)) for r in reader[1:]]
    kinds = sorted(r["event_kind"] for r in body)
    assert kinds == ["committed_effect", "effect"]
    eff = [r for r in body if r["event_kind"] == "effect"][0]
    assert eff["action_effect_tags"] == "net_mutate|send_message"
    assert eff["ts"].endswith("Z") and len(eff["ts"]) == 27   # ISO, fixed width

    m = res["manifest"]
    assert set(m["row_kinds"]) == {"effect", "committed_effect", "gate_decision"}
    assert "action_effect_tags" not in m["columns_never_populated_by_this_build"]
    assert "event_kind" not in m["columns_constant_in_this_build"]
    assert "source_kind" not in m["columns_constant_in_this_build"]
    assert "not_recorded" in m["reading_note"]
    # every operator-facing annotation this slice added is ASCII (the reading note's
    # first sentence predates the slice)
    added_note = m["reading_note"].split("that property. ", 1)[1]
    for text_ in list(ledger_export.ROW_KINDS.values()) + [added_note]:
        text_.encode("ascii")


def test_two_way_pin_holds_with_a_stamped_maximal_row(tmp_path):
    """With a STAMPED, receipted row the effect-tags column is no longer blank, and
    the curated never-populated list still equals the blank set exactly."""
    v = _file_vault(tmp_path)
    _write_audit(v, [_audit("quick_1", 0, "send_email", gate=dict(_SCORED),
                            params={"url": "https://mail.example.com/send"})])
    receipts_store.write_receipt("quick_1", 0, {
        "confirmed": True, "method": "api_readback",
        "stamped_at": "2026-07-19T00:00:00Z"}, data_dir=tmp_path)
    rows = ledger.project(v, data_dir=tmp_path)
    assert set(ledger_export.blank_columns(rows)) == set(ledger_export.UNPOPULATED_COLUMNS)


def test_rows_never_carry_a_parameter_value_through_the_new_fields(tmp_path):
    """The stamp names the decision, never the arguments: a secret-shaped argument
    under a neutral key reaches neither export through any new field."""
    sneaky = "Zq7Z-payroll-db-root-" + "9" * 300
    v = _file_vault(tmp_path)
    _write_audit(v, [_audit("exec_p", 1, "call", gate=dict(_SCORED),
                            params={"code": sneaky})])
    _receipt(tmp_path, "exec_p", 1)
    rows = ledger.project(v, data_dir=tmp_path)
    for blob in (ledger.export_csv(rows), ledger.export_jsonl(rows)):
        assert sneaky.encode() not in blob
        assert b"Zq7Z-payroll" not in blob
