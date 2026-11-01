"""R-P3b ledger rows, round 2 -- the findings of round 1, each fixed and pinned.

a. DEC-31: the action-tool audit hook masked nothing before writing durably.
b. AC1: an action tool in the workflow lane got TWO audit rows per call.
c. The chat lane wrote no audit row, so chat actions never reached the ledger.
d. DEC-13: a shadow-meter (record-only) receipt rendered as "verified".
e. Decision provenance: consume overwrote resolved_at; resolve kept no channel; the
   resume bridge and the command gate's approve-once lost the decision id.
f. The sqlite / postgres audit tables dropped the gate stamp.
g. Receipts landed in a cwd-relative "data" folder.
h. Code-registered tool calls carried no gate record.

Every fix has a named test here that goes red when its production call site is
removed; the mutation list is in the slice report.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.core.models import Tool, ToolType
from systemu.runtime import audit_log, ledger
from systemu.runtime.tool_sandbox import ToolResult, ToolSandbox


def _cells(row):
    return dict(zip(ledger._COLUMNS, ledger._csv_cells(row)))


# Secrets LONGER than every cap on the audit path (the per-value clip is 200 chars),
# so a clip can never be what hides them (DEC-31).
_SHAPED = "ghp_" + "A1b2" * 100                    # 404 chars, a recognised token shape
_KEYED = "Zz9-" * 100                              # 400 chars, under a secret-named key


def _assert_absent(blob: str, secret: str) -> None:
    assert secret not in blob
    for i in range(0, len(secret) - 40, 40):        # no 40-char window survives either
        assert secret[i:i + 40] not in blob, secret[i:i + 40]


# ═════════════════════════════════════════════════════════════════════════════
#  a. DEC-31 -- the action-tool hook masks before it writes
# ═════════════════════════════════════════════════════════════════════════════

def test_action_tool_hook_masks_before_persisting(tmp_path):
    from sharing_on.config import Config
    from systemu.vault.vault import Vault
    v = Vault(root=tmp_path)
    tool = Tool(id="t1", name="post_status", description="d",
                tool_type=ToolType.PYTHON_FUNCTION, is_action_tool=True)
    sb = ToolSandbox(vault_root=tmp_path, vault=v, config=Config())
    sb._after_successful_call(tool=tool, execution_id="exec_mask", objective_id=1,
                              params={"body": f"payload {_SHAPED} end",
                                      "api_key": _KEYED, "to": "ops@example.com"})
    raw = (tmp_path / "audit" / "actions.jsonl").read_text(encoding="utf-8")
    _assert_absent(raw, _SHAPED)
    _assert_absent(raw, _KEYED)
    row = v.query_action_audit(execution_id="exec_mask")[0]
    assert row["params"]["to"] == "ops@example.com"          # non-secret values kept
    assert set(row["params"]) == {"body", "api_key", "to"}   # the arguments are named


# ═════════════════════════════════════════════════════════════════════════════
#  b. AC1 -- exactly one audit row per side-effectful execution, real loop
# ═════════════════════════════════════════════════════════════════════════════

_STAMP = {"gate": "tool", "verdict": "require_approval", "passed_via": "approve_once",
          "decision_id": "dec_ac1", "effect_tags": ["net_mutate"],
          "effect_tags_basis": "scored"}


def _drive_real_tool_call(tmp_path, monkeypatch, *, is_action_tool, namespace=None):
    """ShadowRuntime.execute with the REAL _handle_tool_call (so the action-tool hook
    runs) and the REAL per-call audit row; only the sandbox's execution is stubbed."""
    from unittest.mock import patch
    from sharing_on.config import Config
    from systemu.core.models import Objective, ToolStatus
    from systemu.runtime.shadow_runtime import ShadowRuntime
    import systemu.runtime.shadow_runtime as _sr
    from test_s3_credit_wiring import (_build_entities_objs, _redirect_snapshot_io,
                                       _softpass_outcome)

    monkeypatch.chdir(tmp_path)
    impl = tmp_path / "tools" / "implementations" / "post_status.py"
    impl.parent.mkdir(parents=True, exist_ok=True)
    impl.write_text("def run(target=None):\n    return {'success': True}\n",
                    encoding="utf-8")
    tool = Tool(id="tool_post", name="post_status", description="d",
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
                enabled=True, is_action_tool=is_action_tool,
                implementation_path=str(impl))
    vault, shadow, activity = _build_entities_objs(
        tmp_path, [Objective(id=1, goal="post it", success_criteria="posted")], tool=tool)
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    _redirect_snapshot_io(monkeypatch, tmp_path / "snap_data")
    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request", lambda *a, **k: "card",
                        raising=False)
    runtime = ShadowRuntime(cfg, vault, audit_namespace=namespace)

    async def _exec(impl_path, params, **kw):
        return ToolResult(success=True, parsed={"success": True}, gate=dict(_STAMP))
    monkeypatch.setattr(runtime.sandbox, "execute_tool", _exec)
    monkeypatch.setattr(_sr, "process_completion_claim", _softpass_outcome)
    decisions = [
        {"action": "TOOL_CALL", "tool_name": "post_status",
         "parameters": {"target": "x"}, "completes_objective": 1, "reasoning": "post"},
        {"action": "FAIL", "reason": "only if not credited"},
    ]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        asyncio.run(runtime.execute(shadow, activity))
    return vault


def _rows_for(path: Path, action: str):
    if not path.exists():
        return []
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()]
    return [r for r in rows if r.get("action") == action]


def test_an_action_tool_call_yields_exactly_one_audit_row_and_ledger_row(
        tmp_path, monkeypatch):
    vault = _drive_real_tool_call(tmp_path, monkeypatch, is_action_tool=True)
    rows = _rows_for(tmp_path / "audit" / "actions.jsonl", "post_status")
    assert len(rows) == 1, rows
    assert rows[0]["gate"] == _STAMP
    effects = [r for r in ledger.project(SimpleNamespace(root=Path(vault.root)))
               if r.event_kind == "effect" and r.action["tool"] == "post_status"]
    assert len(effects) == 1


def test_a_non_action_tool_call_still_yields_its_one_row(tmp_path, monkeypatch):
    _drive_real_tool_call(tmp_path, monkeypatch, is_action_tool=False)
    rows = _rows_for(tmp_path / "audit" / "actions.jsonl", "post_status")
    assert len(rows) == 1 and rows[0]["gate"] == _STAMP


def test_a_namespaced_child_run_keeps_one_global_row_and_its_own_row(
        tmp_path, monkeypatch):
    ns = tmp_path / "child_ns"
    _drive_real_tool_call(tmp_path, monkeypatch, is_action_tool=True, namespace=ns)
    assert len(_rows_for(tmp_path / "audit" / "actions.jsonl", "post_status")) == 1
    assert len(_rows_for(ns / "audit" / "actions.jsonl", "post_status")) == 1


# ═════════════════════════════════════════════════════════════════════════════
#  c. the chat lane's actions reach the ledger and the export
# ═════════════════════════════════════════════════════════════════════════════

_QL_BODY = ("def run(**kwargs):\n"
            "    return {'success': True, 'data': {'ok': True}, 'error': None}\n")


def _ql_vault(tmp_path, name, tags):
    from systemu.core.models import ToolStatus
    from systemu.core.utils import generate_id
    from systemu.vault.vault import Vault
    root = tmp_path / "vault"
    for sub in ["tools/implementations", "elder", "notifications"]:
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "tools" / "index.json").write_text("[]", encoding="utf-8")
    vault = Vault(str(root))
    impl = root / "tools" / "implementations" / f"{name}.py"
    impl.write_text(_QL_BODY, encoding="utf-8")
    vault.save_tool(Tool(
        id=generate_id("tool"), name=name, description="t",
        tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED, enabled=True,
        implementation_path=str(impl), forged_by_systemu=False,
        parameter_names=["target"],
        parameters_schema={"type": "object", "properties": {"target": {"type": "string"}},
                           "required": ["target"]},
        effect_tags=list(tags)))
    return vault


def _ql_run(vault, monkeypatch, name, *, target="report", poll=None):
    from systemu.pipelines import quick_task as qt
    if poll is not None:
        monkeypatch.setattr(qt, "_poll_command_choice", poll)

    def _llm(*, system, user, config):
        if json.loads(user)["iteration"] == 1:
            return {"action": "TOOL_CALL", "tool": name,
                    "params": {"target": target}, "reasoning": "r"}
        return {"action": "ANSWER", "answer_md": "done", "completed": True}

    return qt.run_quick_task("do it", None, vault, llm_json=_llm,
                             sandbox=ToolSandbox(str(vault.root), vault=vault,
                                                 config=None),
                             max_iters=3)


def _csv_rows(path):
    import csv
    import io
    rows = list(csv.reader(io.StringIO(Path(path).read_bytes().decode("utf-8"))))
    return [dict(zip(rows[0], r)) for r in rows[1:]]


def test_a_chat_lane_action_appears_in_the_export(tmp_path, monkeypatch):
    """End to end: the real quick lane, real sandbox gate, real audit writer, real
    projection and real export file."""
    from systemu.runtime import ledger_export
    monkeypatch.chdir(tmp_path)
    vault = _ql_vault(tmp_path, "read_report", ["local_read"])
    res = _ql_run(vault, monkeypatch, "read_report")
    assert res.status == "success", res
    out = ledger_export.write_export(vault, fmt="csv", data_dir=tmp_path)
    rows = [r for r in _csv_rows(out["destination_path"])
            if r["action_tool"] == "read_report"]
    assert len(rows) == 1, rows
    r = rows[0]
    assert r["event_kind"] == "effect" and r["source_kind"] == "action_audit"
    assert r["actor_lane"] == "quick" and r["actor_origin"] == "chat"
    assert r["source_id"].startswith("quick_")              # the chat run's own id
    assert r["action_effect_tags"] == "local_read"
    assert (r["gate_verdict"], r["gate_resolved_via"]) == ("allow", "auto:allow")


def test_a_chat_lane_approval_links_the_effect_to_its_decision(tmp_path, monkeypatch):
    """The card is approved once on the chat lane; the effect row names THAT decision
    and the export also holds the decision row. The argument (a secret longer than
    every cap) never reaches either export."""
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.runtime import ledger_export
    monkeypatch.chdir(tmp_path)
    vault = _ql_vault(tmp_path, "sync_ledger", [])               # UNKNOWN -> card
    target = "ghp_" + "Q9" * 160

    def _approve(v, dedup_key, timeout=None):
        q = OperatorDecisionQueue(vault)
        card = [d for d in q.list_pending() if d.dedup_key == dedup_key][0]
        q.resolve(card.id, choice="Approve once")
        return "Approve once"

    res = _ql_run(vault, monkeypatch, "sync_ledger", target=target, poll=_approve)
    assert res.status == "success", res
    out = ledger_export.write_export(vault, fmt="csv", data_dir=tmp_path)
    rows = _csv_rows(out["destination_path"])
    effect = [r for r in rows if r["action_tool"] == "sync_ledger"
              and r["event_kind"] == "effect"]
    decision = [r for r in rows if r["event_kind"] == "gate_decision"]
    assert len(effect) == 1 and len(decision) == 1, rows
    assert effect[0]["gate_decision_id"] == decision[0]["gate_decision_id"]
    assert decision[0]["outcome_status"] == "Approve once"
    assert effect[0]["action_effect_tags"] == "unknown"
    blob = Path(out["destination_path"]).read_bytes()
    assert target.encode() not in blob and target[:40].encode() not in blob
    raw = (Path(vault.root) / "audit" / "actions.jsonl").read_text(encoding="utf-8")
    _assert_absent(raw, target)                                  # masked at the source


# ═════════════════════════════════════════════════════════════════════════════
#  d. DEC-13 -- a record-only measurement is never "verified"
# ═════════════════════════════════════════════════════════════════════════════

def _write_audit(root: Path, entries):
    (root / "audit").mkdir(parents=True, exist_ok=True)
    with (root / "audit" / "actions.jsonl").open("a", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def _audit_row(eid, oid=1, ts="2026-10-01T10:00:00Z"):
    return {"ts": ts, "execution_id": eid, "objective_id": oid, "action": "create_row",
            "params": {"target": "x"}, "success": True, "error": None}


@pytest.mark.parametrize("confirmed", [True, False])
def test_a_shadow_meter_receipt_renders_measured_never_verified(tmp_path, confirmed):
    from systemu.runtime import receipts_store
    root = tmp_path / "v"
    _write_audit(root, [_audit_row("exec_meter")])
    receipts_store.write_receipt("exec_meter", 1, {
        "objective_id": 1, "confirmed": confirmed, "method": "api_readback",
        "detail": "row 3", "stamped_at": "2026-10-01T10:00:02Z", "shadow": True},
        data_dir=tmp_path)
    rows = ledger.project(SimpleNamespace(root=root), data_dir=tmp_path)
    assert [r.event_kind for r in rows] == ["effect"]          # and never committed
    row = rows[0]
    assert row.outcome["verification"] == ledger.MEASURED == "measured"
    assert row.outcome["would_credit"] is confirmed
    assert _cells(row)["outcome_verification"] == "measured"
    assert b"verified" not in ledger.export_csv(rows)


def test_live_and_legacy_receipts_keep_verified_and_claimed(tmp_path):
    from systemu.runtime import receipts_store
    root = tmp_path / "v"
    _write_audit(root, [_audit_row("exec_live"), _audit_row("exec_claim")])
    receipts_store.write_receipt("exec_live", 1, {
        "objective_id": 1, "confirmed": True, "method": "api_readback",
        "stamped_at": "2026-10-01T10:00:02Z", "shadow": False}, data_dir=tmp_path)
    receipts_store.write_receipt("exec_claim", 1, {
        "objective_id": 1, "confirmed": False, "method": "self_report",
        "shadow": False}, data_dir=tmp_path)
    by = {r.actor["run_ref"]["execution_id"]: r.outcome["verification"]
          for r in ledger.project(SimpleNamespace(root=root), data_dir=tmp_path)
          if r.event_kind == "effect"}
    assert by == {"exec_live": "verified", "exec_claim": "claimed"}


def test_the_receipt_badge_never_calls_a_measurement_verified(tmp_path):
    from systemu.runtime import receipts_store
    receipts_store.write_receipt("exec_b", 1, {
        "objective_id": 1, "confirmed": True, "method": "api_readback",
        "detail": "row 3", "shadow": True}, data_dir=tmp_path)
    (badge,) = receipts_store.receipt_badges_for("exec_b", data_dir=tmp_path)
    assert badge["verified"] is False and badge["label"] == "Measured"
    assert "did not credit" in badge["tooltip"]


# ═════════════════════════════════════════════════════════════════════════════
#  e. decision provenance: resolution time, channel, and the bridge's decision
# ═════════════════════════════════════════════════════════════════════════════

def _real_vault(tmp_path):
    from systemu.vault.vault import Vault
    return Vault(str(tmp_path / "vault"))


def _post_gate(q, *, dedup="tool:x", **ctx):
    return q.post(title="t", body="b", options=["Deny", "Approve once"],
                  context={"kind": "gate", "gate_type": "tool", "tool_name": "x",
                           "verdict": "require_approval", "execution_id": "exec_d",
                           **ctx},
                  dedup_key=dedup)


def _decision_rows(v, tmp_path):
    return {r.source_id: r for r in ledger.project(v, data_dir=tmp_path)
            if r.event_kind == "gate_decision"}


def test_consuming_a_decision_keeps_its_resolution_time(tmp_path):
    import time
    from systemu.approval.decision_queue import OperatorDecisionQueue
    v = _real_vault(tmp_path)
    q = OperatorDecisionQueue(v)
    did = _post_gate(q)
    resolved_at = q.resolve(did, choice="Approve once").resolved_at
    time.sleep(0.01)
    consumed = q.consume_resolved_decision("tool:x")
    stored = v.get_decision(did)
    assert consumed.id == did and stored.status == "consumed"
    assert stored.resolved_at == resolved_at                 # NOT overwritten
    assert stored.context["consumed_at"] > resolved_at.isoformat()
    row = _decision_rows(v, tmp_path)[did]
    assert row.ts == ledger.norm_ts(resolved_at.isoformat())
    assert row.outcome["consumed_at"] == ledger.norm_ts(stored.context["consumed_at"])


def test_a_card_consumed_before_the_split_says_its_ts_is_the_consumption(tmp_path):
    from systemu.approval.decision_queue import OperatorDecisionQueue
    v = _real_vault(tmp_path)
    q = OperatorDecisionQueue(v)
    did = _post_gate(q)
    q.resolve(did, choice="Approve once")
    d = v.get_decision(did)
    d.status = "consumed"                                      # the old shape on disk
    v.save_decision(d)
    row = _decision_rows(v, tmp_path)[did]
    assert "consumption time" in row.outcome["not_recorded_because"]["consumed_at"]


def test_the_cli_names_itself_when_it_resolves(tmp_path):
    from unittest.mock import MagicMock, patch
    from click.testing import CliRunner
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.interface.cli_commands import decisions_group
    v = _real_vault(tmp_path)
    did = OperatorDecisionQueue(v).post(title="t", body="b", options=["Skip", "Forge"],
                                        context={"tool_id": "t1"}, dedup_key="f:t1")
    with patch("systemu.interface.cli_commands._get_vault_and_config",
               return_value=(MagicMock(), v)):
        out = CliRunner().invoke(decisions_group, ["resolve", did, "--choice", "Forge"])
    assert out.exit_code == 0, out.output
    assert v.get_decision(did).context["resolved_via"] == "cli"


def test_the_dashboard_inbox_names_itself_when_it_resolves(tmp_path):
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.interface.pages.inbox_page import _resolve_and_execute_confirmed_gate
    v = _real_vault(tmp_path)
    did = _post_gate(OperatorDecisionQueue(v))
    _resolve_and_execute_confirmed_gate(did, "Deny", v, None)
    assert v.get_decision(did).context["resolved_via"] == "dashboard"
    assert _cells(_decision_rows(v, tmp_path)[did])["gate_resolved_via"] == "dashboard"


def test_the_rail_names_itself_when_it_resolves(tmp_path):
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.interface.components import inbox_rail
    v = _real_vault(tmp_path)
    q = OperatorDecisionQueue(v)
    did = q.post(title="q", body="b", options=["Submit"],
                 context={"kind": "structured_question"}, dedup_key="ask:1")
    inbox_rail._decline_ask(did, vault=v)
    assert v.get_decision(did).context["resolved_via"] == "dashboard"


def test_the_phone_bridge_records_its_channel_on_the_card(tmp_path, monkeypatch):
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.messaging import decision_bridge as db
    monkeypatch.setenv("SHARING_ON_TELEGRAM_ALLOWED_USER_IDS", "111")
    db._rate_hits.clear()
    v = _real_vault(tmp_path)
    q = OperatorDecisionQueue(v)
    did = _post_gate(q, resolution_class="remotely_resolvable")
    out, _msg = db.resolve_from_channel(db.decision_tag(did), "a2", sender_id="111",
                                        channel="telegram", queue=q)
    db._rate_hits.clear()
    assert out == "OK"
    assert v.get_decision(did).context["resolved_via"] == "telegram"


def test_the_gate_mode_policy_names_itself(tmp_path):
    from systemu.interface.command.gate import GateDescriptor
    from systemu.interface.command.inbox import InboxQueue
    v = _real_vault(tmp_path)

    class _Deny:
        def decide(self, **kw):
            return "deny"

    class _Allow:
        def decide(self, **kw):
            return "allow"

    desc = GateDescriptor.from_command(tool_name="run_command", command="dir", cwd="")
    deny_id = InboxQueue(v).enqueue(desc, gate_type="command", policy=_Deny())
    assert v.get_decision(deny_id).context["resolved_via"] == "auto:policy"
    InboxQueue(v).enqueue(GateDescriptor.from_command(tool_name="run_command",
                                                      command="ver", cwd=""),
                          gate_type="command", policy=_Allow())
    vias = {d.context.get("resolved_via") for d in
            (v.get_decision(h["id"]) for h in v.load_index("decisions"))}
    assert vias == {"auto:policy"}


def test_a_stamped_channel_wins_over_an_older_witness(tmp_path):
    from systemu.approval.decision_queue import OperatorDecisionQueue, resolution_channel
    from systemu.messaging import decision_bridge
    v = _real_vault(tmp_path)
    q = OperatorDecisionQueue(v)
    did = _post_gate(q)
    with resolution_channel("dashboard"):
        q.resolve(did, choice="Deny")
    decision_bridge._audit(q, {"decision_id": did, "channel": "telegram",
                               "outcome": "OK"})
    assert _cells(_decision_rows(v, tmp_path)[did])["gate_resolved_via"] == "dashboard"


def _resolve_calls_without_a_channel(root: Path):
    """Every production call that resolves a decision and is NOT inside a
    ``resolution_channel(...)`` block (``_via`` at the call sites)."""
    import ast
    unnamed = []
    for py in sorted(root.rglob("*.py")):
        rel = py.relative_to(root).as_posix()
        if rel == "approval/decision_queue.py":
            continue
        tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        def _named(node):
            while node in parents:
                node = parents[node]
                if isinstance(node, ast.With):
                    for item in node.items:
                        func = getattr(item.context_expr, "func", None)
                        if getattr(func, "id", None) in ("_via", "resolution_channel"):
                            return True
                if (isinstance(node, ast.FunctionDef)
                        and node.name == "resolve_pending_gate"):
                    return True
            return False

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            hit = False
            if isinstance(func, ast.Attribute):
                if func.attr == "resolve_with_context_patch":
                    hit = True
                elif func.attr == "resolve" and (
                        any(k.arg == "choice" for k in node.keywords)
                        or rel == "messaging/decision_bridge.py"):
                    hit = True
            elif isinstance(func, ast.Name) and func.id == "resolve_fn":
                hit = True
            if hit and not _named(node):
                unnamed.append(f"{rel}:{node.lineno}")
    return unnamed


@pytest.mark.source_sensitive
def test_every_production_resolve_call_names_its_channel():
    """THE COMPLETENESS PIN. Every production call that resolves a decision sits
    inside a ``resolution_channel(...)`` block, except ``inbox.resolve_pending_gate``
    whose only caller does. A new resolve call that names no surface fails here."""
    root = Path(__file__).resolve().parent.parent / "systemu"
    assert _resolve_calls_without_a_channel(root) == []
    callers = sorted(p.relative_to(root).as_posix() for p in root.rglob("*.py")
                     if "resolve_pending_gate(" in p.read_text(encoding="utf-8",
                                                               errors="replace")
                     and p.name != "inbox.py")
    assert callers == ["interface/pages/inbox_page.py"]


def test_a_resume_bridge_carries_the_decision_that_minted_it(tmp_path, monkeypatch):
    """Real gate -> real card -> real resume dispatcher -> the bridge it mints ->
    the gate that redeems it names THAT decision."""
    import systemu.runtime.chat_submission_ctx as csc
    import systemu.runtime.command_approvals as ca
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.approval.exceptions import PendingOperatorDecision
    from systemu.runtime import resume_on_decision as rod
    from test_rp3b_ledger_rows import _MemVault, _run, _sandbox, _tool

    for name, val in (("current_execution_id", "exec_B"),
                      ("current_chat_submission_id", None),
                      ("current_activity_id", "act_B"), ("current_shadow_id", "sh_B")):
        monkeypatch.setattr(csc, name, lambda _v=val: _v, raising=False)
    vault = _MemVault(root=tmp_path)
    sb, store = _sandbox(tmp_path, vault)
    rod._handled.clear()
    monkeypatch.setattr(ca, "init_default_store", lambda _p: store)
    tool = _tool(tmp_path, "file_report", [])

    class _Sup:
        def submit(self, *a, **k):
            pass

    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, {"target": "q4"})
    card = OperatorDecisionQueue(vault).list_pending()[0]
    resolved = OperatorDecisionQueue(vault).resolve(card.id, choice="Approve once")
    assert rod._dispatch_resume(resolved, vault=vault, supervisor=_Sup(),
                                data_dir=str(tmp_path)) is True
    res = _run(sb, tool, {"target": "q4"})
    assert res.success, res.error
    assert (res.gate["passed_via"], res.gate["decision_id"]) == ("resume_bridge", card.id)


def test_a_command_bridge_carries_its_decision_too(tmp_path, monkeypatch):
    import systemu.runtime.command_approvals as ca
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.approval.exceptions import PendingOperatorDecision
    from systemu.runtime import resume_on_decision as rod
    from test_rp3b_ledger_rows import _MemVault, _impl, _sandbox

    vault = _MemVault(root=tmp_path)
    sb, store = _sandbox(tmp_path, vault)
    monkeypatch.setattr(ca, "init_default_store", lambda _p: store)
    impl = _impl(tmp_path, "run_command", body_extra="shell")
    rel = str(impl.relative_to(tmp_path))
    params = {"command": "del old.tmp"}
    with pytest.raises(PendingOperatorDecision):
        asyncio.run(sb.execute_tool(rel, params))
    card = OperatorDecisionQueue(vault).list_pending()[0]
    rod._record_gate_approval(card.context, is_tool_gate=False, choice="approve once",
                              decision_id=card.id)
    res = asyncio.run(sb.execute_tool(rel, params))
    assert res.success, res.error
    assert (res.gate["passed_via"], res.gate["decision_id"]) == ("resume_bridge", card.id)


def test_the_patch_resolve_path_records_the_channel_too(tmp_path):
    from systemu.approval.decision_queue import OperatorDecisionQueue, resolution_channel
    v = _real_vault(tmp_path)
    q = OperatorDecisionQueue(v)
    did = _post_gate(q)
    with resolution_channel("dashboard"):
        q.resolve_with_context_patch(did, choice="Approve once",
                                     context_patch={"amended": True})
    ctx = v.get_decision(did).context
    assert ctx["resolved_via"] == "dashboard" and ctx["amended"] is True
    did2 = _post_gate(q, dedup="tool:y")
    q.resolve(did2, choice="Deny")                         # no surface named
    assert "resolved_via" not in v.get_decision(did2).context


# ═════════════════════════════════════════════════════════════════════════════
#  f. the sqlite / postgres audit tables keep the gate stamp (DEC-9 migration)
# ═════════════════════════════════════════════════════════════════════════════

_OLD_AUDIT_DDL = """
CREATE TABLE action_audit (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    user_id       TEXT,
    execution_id  TEXT    NOT NULL,
    objective_id  INTEGER NOT NULL,
    action        TEXT    NOT NULL,
    params_json   TEXT    NOT NULL,
    success       INTEGER NOT NULL,
    error         TEXT
);
"""


def test_sqlite_store_that_predates_the_column_migrates_and_round_trips(tmp_path):
    """An EXISTING store (old table, an old row) migrates when the vault opens; the
    new row round-trips its gate stamp and the old row reads not_recorded."""
    import sqlite3
    from systemu.storage.sqlite.vault import SqliteVault
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(_OLD_AUDIT_DDL)
    conn.execute("INSERT INTO action_audit (ts, user_id, execution_id, objective_id, "
                 "action, params_json, success, error) VALUES "
                 "('2026-09-01T10:00:00Z', NULL, 'exec_sq', 0, 'old_action', '{}', 1, NULL)")
    conn.commit()
    conn.close()

    sv = SqliteVault(f"sqlite:///{db.as_posix()}", memory_dir=tmp_path / "mem")
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(action_audit)")}
    assert "gate_json" in cols
    audit_log.append_action(sv, execution_id="exec_sq", objective_id=0,
                            action="new_action", params={}, success=True,
                            gate=dict(_STAMP))
    got = {r["action"]: r for r in sv.query_action_audit(execution_id="exec_sq")}
    assert "gate" not in got["old_action"]
    assert got["new_action"]["gate"] == _STAMP
    rows = {r.action["tool"]: _cells(r)
            for r in ledger.project(sv, execution_id="exec_sq", data_dir=tmp_path)}
    assert rows["old_action"]["gate_verdict"] == ledger.NOT_RECORDED
    assert rows["new_action"]["gate_verdict"] == "require_approval"
    assert rows["new_action"]["action_effect_tags"] == "net_mutate"


def test_a_fresh_sqlite_store_has_an_action_audit_table_at_all(tmp_path):
    """Before this, nothing in production created the table, so every append on a
    sqlite store failed and was swallowed."""
    from systemu.storage.sqlite.vault import SqliteVault
    sv = SqliteVault(f"sqlite:///{(tmp_path / 'fresh.db').as_posix()}",
                     memory_dir=tmp_path / "mem")
    audit_log.append_action(sv, execution_id="exec_f", objective_id=0, action="a",
                            params={}, success=True, gate=dict(_STAMP))
    assert sv.query_action_audit(execution_id="exec_f")[0]["gate"] == _STAMP


class _PgCursor:
    def __init__(self, log, rows=()):
        self.log, self._rows = log, list(rows)

    def execute(self, sql, args=None):
        self.log.append((" ".join(sql.split()), args))

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class _PgConn:
    def __init__(self, log, rows=()):
        self.log, self.rows = log, rows

    def cursor(self):
        return _PgCursor(self.log, self.rows)

    def commit(self):
        pass

    def close(self):
        pass


def test_postgres_ddl_adds_the_gate_column_new_and_migrated():
    """DDL-level, like the other pg-DDL pins: no server needed. A fresh table gets
    the column; an existing one gets it from an IF NOT EXISTS migration that runs
    AFTER the CREATE, on every ensure_schema."""
    from systemu.vault.backend import postgres_backend as pg
    assert "gate_json     JSONB" in pg._ACTION_AUDIT_DDL
    assert pg._ACTION_AUDIT_MIGRATIONS == [
        "ALTER TABLE action_audit ADD COLUMN IF NOT EXISTS gate_json JSONB"]


def test_postgres_ensure_schema_runs_the_migration_after_the_create(monkeypatch):
    from systemu.vault.backend import postgres_backend as pg
    log = []
    monkeypatch.setattr(pg, "_connect", lambda v: _PgConn(log))
    pg.ensure_schema(SimpleNamespace())
    stmts = [s for s, _ in log]
    create = next(i for i, s in enumerate(stmts)
                  if s.startswith("CREATE TABLE IF NOT EXISTS action_audit"))
    alter = stmts.index("ALTER TABLE action_audit ADD COLUMN IF NOT EXISTS gate_json JSONB")
    assert create < alter


def test_postgres_append_and_query_round_trip_the_stamp(monkeypatch):
    from systemu.vault.backend import postgres_backend as pg
    log = []
    monkeypatch.setattr(pg, "psycopg2", object(), raising=False)
    monkeypatch.setattr(pg, "_connect", lambda v: _PgConn(log))
    pg.dispatch_append_action_audit(SimpleNamespace(), {
        "ts": "t", "execution_id": "e", "objective_id": 0, "action": "a",
        "params": {}, "success": True, "error": None, "gate": dict(_STAMP)})
    sql, args = log[-1]
    assert "gate_json" in sql and sql.count("::jsonb") == 2
    assert json.loads(args[-1]) == _STAMP
    rows = [("t", None, "e", 0, "new", {}, True, None, dict(_STAMP)),
            ("t", None, "e", 0, "text", {}, True, None, json.dumps(_STAMP)),
            ("t", None, "e", 0, "old", {}, True, None, None)]
    monkeypatch.setattr(pg, "_connect", lambda v: _PgConn(log, rows))
    got = {r["action"]: r for r in pg.dispatch_query_action_audit(
        SimpleNamespace(), execution_id="e")}
    assert got["new"]["gate"] == _STAMP and got["text"]["gate"] == _STAMP
    assert "gate" not in got["old"]
    assert "gate_json FROM action_audit" in log[-1][0]


@pytest.mark.skipif(not __import__("os").getenv("SYSTEMU_POSTGRES_URL"),
                    reason="set SYSTEMU_POSTGRES_URL to run postgres tests")
def test_postgres_live_round_trip():
    import os
    from systemu.vault.backend import postgres_backend as pg
    v = SimpleNamespace(_postgres_url=os.environ["SYSTEMU_POSTGRES_URL"])
    pg.ensure_schema(v)
    pg.dispatch_append_action_audit(v, {
        "ts": "2026-10-01T00:00:00Z", "execution_id": "exec_pg_rp3b",
        "objective_id": 0, "action": "a", "params": {}, "success": True,
        "error": None, "gate": dict(_STAMP)})
    got = pg.dispatch_query_action_audit(v, execution_id="exec_pg_rp3b")
    assert got[-1]["gate"] == _STAMP


# ═════════════════════════════════════════════════════════════════════════════
#  g. receipts live under the audit data root, whatever the working directory
# ═════════════════════════════════════════════════════════════════════════════

def test_receipts_are_anchored_to_the_operating_home_not_the_cwd(tmp_path, monkeypatch):
    from systemu.runtime import receipts_store
    from systemu.runtime.vault_root import resolve_vault_root
    vault_dir = tmp_path / "home" / "systemu" / "vault"
    vault_dir.mkdir(parents=True)
    monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(vault_dir))
    home = Path(resolve_vault_root().operating_home)
    writer_cwd, reader_cwd = tmp_path / "writer", tmp_path / "reader"
    writer_cwd.mkdir()
    reader_cwd.mkdir()

    monkeypatch.chdir(writer_cwd)                       # the daemon's cwd, say
    receipts_store.write_receipt("exec_anchor", 1, {
        "objective_id": 1, "confirmed": True, "method": "api_readback",
        "detail": "row 9", "stamped_at": "2026-10-01T10:00:05Z", "shadow": False})
    target = home / "data" / "audit" / "exec_exec_anchor" / "receipts.json"
    assert target.is_file()
    assert not (writer_cwd / "data").exists()

    monkeypatch.chdir(reader_cwd)                       # the CLI / an export, say
    assert receipts_store.read_receipts("exec_anchor")["1"]["confirmed"] is True
    root = tmp_path / "ledger_vault"
    _write_audit(root, [_audit_row("exec_anchor")])
    kinds = {r.event_kind: r.outcome["verification"]
             for r in ledger.project(SimpleNamespace(root=root))}
    assert kinds == {"effect": "verified", "committed_effect": "verified"}
    assert not (reader_cwd / "data").exists()


def test_an_explicit_data_dir_still_wins(tmp_path):
    from systemu.runtime import receipts_store
    receipts_store.write_receipt("exec_x", 1, {"objective_id": 1, "confirmed": True},
                                 data_dir=tmp_path / "explicit")
    assert (tmp_path / "explicit" / "audit" / "exec_exec_x" / "receipts.json").is_file()


# ═════════════════════════════════════════════════════════════════════════════
#  h. code-registered tool calls say "not recorded", with the reason
# ═════════════════════════════════════════════════════════════════════════════

def test_a_code_registered_call_carries_the_no_gate_marker(tmp_path, monkeypatch):
    """The REAL v2 dispatch (write_file, a code-registered tool): no per-tool gate
    runs there, and the result says exactly that."""
    import systemu.runtime.tools.file_tools  # noqa: F401  (registers write_file)
    from systemu.runtime.shadow_runtime import ShadowRuntime
    from systemu.runtime.context_builder import ExecutionContext

    class _Cfg:
        def __init__(self, out):
            self.output_dir = out
            self.check_fn_cache_ttl_seconds = 30
            self.capability_track_outcomes = False

    monkeypatch.chdir(tmp_path)
    rt = ShadowRuntime.__new__(ShadowRuntime)
    out = tmp_path / "output"
    out.mkdir()
    rt.config = _Cfg(str(out))
    rt.vault = None
    rt.sandbox = ToolSandbox(vault_root=tmp_path, backend="local", vault=None,
                             config=rt.config)
    rt._dep_failed_tools, rt._consec_tool_fails = {}, {}
    rt._fresh_work_since_last_verifier_call = False
    rt._execution_mind = None
    ctx = ExecutionContext(execution_id="exec_v2", system_prompt="", scroll_json=[],
                           tool_index=[])
    res = asyncio.run(rt._handle_tool_call(
        {"decision": "TOOL_CALL", "tool_name": "write_file",
         "parameters": {"path": "r.txt", "content": "x"}, "reasoning": "r"},
        tools=[], context=ctx, current_ab=0, dry_run=False))
    assert res is not None and res.success, res
    assert res.gate == {"gate": None, "verdict": None, "passed_via": "code_registered",
                        "decision_id": None, "effect_tags": None,
                        "effect_tags_basis": None}


def test_a_code_registered_row_reads_not_recorded_with_the_reason(tmp_path):
    root = tmp_path / "v"
    row = _audit_row("exec_v2row")
    row["gate"] = {"gate": None, "verdict": None, "passed_via": "code_registered",
                   "decision_id": None, "effect_tags": None, "effect_tags_basis": None}
    _write_audit(root, [row])
    (r,) = ledger.project(SimpleNamespace(root=root), data_dir=tmp_path)
    c = _cells(r)
    for col in ("gate_verdict", "gate_decision_id", "gate_resolved_via",
                "action_effect_tags"):
        assert c[col] == ledger.NOT_RECORDED, col
    assert "code-registered" in r.gate["not_recorded_because"]["verdict"]
    assert "does not score" in r.action["not_recorded_because"]["effect_tags"]
