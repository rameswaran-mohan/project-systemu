"""v0.9.37 Bug 11 — request-outcome reconciliation across the suspend→resume lifecycle.

A HIGH-risk request (TOOL forge / MCP attach / SUBAGENT) escalates → suspends →
operator approves → resumes → uses the capability. That lifecycle splits across
TWO execution ids: the request + escalate/grant arb rows + lease-mint live in the
ORIGINAL (pre-suspend) exec's ledger (the approve path calls
``materialise(..., execution_id=<original>)``), while the capability is USED in the
RESUMED run. Before this fix the terminal finalize reconciled only the *fresh
resumed* exec (empty ledger) → no ``request-outcome`` → RQ1's pull taxonomy was
blind for every HIGH-risk family.

Fix (three coupled changes):
  1. ``reconcile_outcomes`` collapses multiple arb rows for one request_id to a
     single outcome, preferring grant > deny > escalate (so escalate+grant → one
     granted_*).
  2. ``write_outcome_reconciliation`` accepts ``also_ids`` (the original exec) and
     is idempotent per request_id.
  3. The terminal finalize reconciles the resumed run's tools against BOTH the
     resumed and the original ledgers; suspends defer reconciliation
     (``reconcile=False``) so no premature escalate_unresolved double-counts.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch

from systemu.runtime.governor import Governor
from systemu.core.models import (
    Activity, Shadow, ShadowStatus, Tool, ToolStatus, ToolType,
    Scroll, Objective, HarnessVerdict, HarnessDecision, RiskBand,
)
from systemu.vault.vault import Vault
from systemu.runtime.execution_snapshot import ExecutionSnapshot, write_snapshot


# ─────────────────────────────────────────────────────────────────────────────
# Part 1 — reconcile_outcomes collapses escalate+grant for one request_id
# ─────────────────────────────────────────────────────────────────────────────

def test_reconcile_outcomes_collapses_escalate_then_grant():
    """The escalate (suspend) row + grant (approve) row for the SAME request_id
    yield exactly ONE outcome = granted_used (grant wins over escalate)."""
    rows = [
        {"request": {"request_id": "X", "attempts_before": 1},
         "verdict": {"decision": "escalate"}, "execution_id": "orig"},
        {"request": {"request_id": "X", "attempts_before": 1},
         "verdict": {"decision": "grant"}, "outcome": {"tool": "geocode"},
         "execution_id": "orig"},
        {"request": {"request_id": "Y"},
         "verdict": {"decision": "escalate"}, "execution_id": "orig"},
    ]
    ev = Governor.reconcile_outcomes(rows, {"geocode"}, run_success=True)
    by = {e["request_id"]: e["outcome"] for e in ev}
    assert len(ev) == 2, f"expected one outcome per request_id, got {ev}"
    assert by["X"] == "granted_used"          # escalate+grant collapsed → granted_used
    assert by["Y"] == "escalate_unresolved"   # lone escalate unchanged


def test_reconcile_outcomes_grant_unused_when_tool_not_called():
    rows = [
        {"request": {"request_id": "X"}, "verdict": {"decision": "escalate"}},
        {"request": {"request_id": "X"}, "verdict": {"decision": "grant"},
         "outcome": {"tool": "geocode"}},
    ]
    ev = Governor.reconcile_outcomes(rows, set(), run_success=True)  # tool NOT used
    assert [e["outcome"] for e in ev] == ["granted_unused"]


# ─────────────────────────────────────────────────────────────────────────────
# Part 2 — write_outcome_reconciliation folds in the original exec via also_ids
# ─────────────────────────────────────────────────────────────────────────────

class _Vault:
    def __init__(self, root):
        self.root = root


def _seed_arb(gov, vault, eid, request_id, decision, tool=None):
    row = {"request": {"request_id": request_id, "attempts_before": 1},
           "verdict": {"decision": decision}, "execution_id": eid}
    if tool is not None:
        row["outcome"] = {"tool": tool}
    gov._ledger_append(row, vault=vault, execution_id=eid)


def test_write_reconciliation_also_ids_classifies_granted_used(tmp_path):
    """The resumed exec's ledger is empty; the original exec holds the
    escalate+grant arb rows. Reconciling the resumed run's tools against BOTH
    (via also_ids) writes ONE granted_used — and a re-run writes nothing
    (idempotent per request_id)."""
    vault = _Vault(tmp_path)
    gov = Governor(config=None)
    # ORIGINAL (pre-suspend) exec: escalate at suspend, then grant at approve.
    _seed_arb(gov, vault, "exec_orig", "req1", "escalate")
    _seed_arb(gov, vault, "exec_orig", "req1", "grant", tool="geocode")

    n = gov.write_outcome_reconciliation(
        "exec_resumed", {"geocode"}, run_success=True, vault=vault,
        also_ids=["exec_orig"])
    assert n == 1, "expected exactly one request-outcome written"

    outs = [r for r in gov._read_ledger_rows("exec_resumed", vault)
            if r.get("event_type") == "request-outcome"]
    assert len(outs) == 1
    assert outs[0]["request_id"] == "req1"
    assert outs[0]["outcome"] == "granted_used"

    # Idempotency: a second finalize must NOT append a duplicate.
    n2 = gov.write_outcome_reconciliation(
        "exec_resumed", {"geocode"}, run_success=True, vault=vault,
        also_ids=["exec_orig"])
    assert n2 == 0, "re-reconcile double-wrote (not idempotent per request_id)"
    outs2 = [r for r in gov._read_ledger_rows("exec_resumed", vault)
             if r.get("event_type") == "request-outcome"]
    assert len(outs2) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Part 3 — execute() wiring: resume reconciles both ids; suspend defers
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_vault(tmp_path):
    for sub in ["scrolls", "activities", "shadow_army", "skills",
                "tools/implementations", "evolutions", "notifications",
                "executions", "decisions"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx_dir in ["scrolls", "activities", "shadow_army", "skills",
                    "tools", "evolutions", "decisions"]:
        (tmp_path / idx_dir / "index.json").write_text("[]", encoding="utf-8")
    (tmp_path / "global_memory.jsonl").write_text("", encoding="utf-8")
    return Vault(str(tmp_path))


@pytest.fixture
def mock_config(tmp_path):
    from sharing_on.config import Config
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    return cfg


@pytest.fixture
def runtime_setup(tmp_vault):
    shadow = Shadow(id="shadow_b11", name="Bug11 Shadow", description="t",
                    system_prompt="t", status=ShadowStatus.AWAKENED)
    tmp_vault.save_shadow(shadow)
    tool = Tool(id="tool_b11", name="seed_tool", description="t",
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
                enabled=True,
                implementation_path="vault/tools/implementations/seed_tool.py")
    tmp_vault.save_tool(tool)
    scroll = Scroll(id="scroll_b11", name="Bug11 Scroll", source_session_id="s",
                    raw_instructions_path="", narrative_md="",
                    objectives=[Objective(id=1, goal="Use capability",
                                          success_criteria="Done")])
    tmp_vault.save_scroll(scroll)
    activity = Activity(id="act_b11", name="Bug11 Activity", scroll_id=scroll.id,
                        required_tool_ids=["tool_b11"], required_skill_ids=[],
                        assigned_shadow_id=shadow.id)
    tmp_vault.save_activity(activity)
    return shadow, activity, scroll, tool


def _redirect_snapshot(monkeypatch, tmp_path):
    data_dir = tmp_path / "snap_data"
    (data_dir / "audit").mkdir(parents=True, exist_ok=True)
    import systemu.runtime.execution_snapshot as _es
    rw, rr, rd = _es.write_snapshot, _es.read_snapshot, _es.delete_snapshot
    monkeypatch.setattr(_es, "write_snapshot", lambda snap, **kw: rw(snap, data_dir=data_dir))
    monkeypatch.setattr(_es, "read_snapshot", lambda eid, **kw: rr(eid, data_dir=data_dir))
    monkeypatch.setattr(_es, "delete_snapshot", lambda eid, **kw: rd(eid, data_dir=data_dir))
    return data_dir


class _RecordingGovernor:
    """Records write_outcome_reconciliation calls (execution_id + also_ids)."""
    reconcile_calls: list = []

    def __init__(self, *a, **k):
        pass

    def arbitrate(self, *a, **k):  # pragma: no cover - resume doesn't re-arbitrate
        raise AssertionError("resume must not re-arbitrate")

    def materialise(self, *a, **k):  # pragma: no cover
        raise AssertionError("resume must not re-materialise")

    def revoke_leases(self, *a, **k):
        return 0

    def write_outcome_reconciliation(self, execution_id, used, *, run_success=True,
                                     vault=None, also_ids=None):
        type(self).reconcile_calls.append(
            {"execution_id": execution_id, "also_ids": list(also_ids or [])})
        return 0


@pytest.mark.asyncio
async def test_resume_terminal_reconciles_original_exec_id(
        tmp_vault, mock_config, runtime_setup, tmp_path, monkeypatch):
    """A resumed run's terminal finalize reconciles with also_ids carrying the
    ORIGINAL (resume_from) exec id — the ledger that actually holds the request."""
    shadow, activity, scroll, tool = runtime_setup
    data_dir = _redirect_snapshot(monkeypatch, tmp_path)
    _RecordingGovernor.reconcile_calls = []
    exec_id = "exec_orig_b11"
    # Minimal resume snapshot (a COMPUTE grant — lightweight to replay).
    import json as _json
    snap = ExecutionSnapshot(
        execution_id=exec_id, shadow_id="shadow_b11", scroll_id="scroll_b11",
        activity_id="act_b11", iteration=1, completed_objective_ids=[],
        sticky_notes=[
            f"__HARNESS_PENDING__::{exec_id}::" + _json.dumps(
                {"request_id": "h1", "kind": "compute", "spec": {}, "fallback": "x"}),
            f"__HARNESS_GRANT__::{exec_id}::" + _json.dumps(
                {"kind": "compute", "compute_grant": {"extra_iterations": 3}}),
        ],
    )
    write_snapshot(snap, data_dir=data_dir)

    decisions = [{"action": "FAIL", "reason": "done — checking reconcile wiring"}]

    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.governor.Governor", _RecordingGovernor), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(mock_config, tmp_vault)
        await runtime.execute(shadow, activity, resume_from_execution_id=exec_id)

    assert _RecordingGovernor.reconcile_calls, "terminal finalize never reconciled"
    # the original exec id is folded in via also_ids on the terminal reconcile
    assert any(exec_id in c["also_ids"] for c in _RecordingGovernor.reconcile_calls), \
        f"resume_from_execution_id not passed as also_ids: {_RecordingGovernor.reconcile_calls}"


class _EscalateRecordingGovernor(_RecordingGovernor):
    def arbitrate(self, req, context=None):
        return HarnessVerdict(request_id=req.request_id,
                              decision=HarnessDecision.ESCALATE,
                              risk_band=RiskBand.HIGH, rationale="needs approval")


@pytest.mark.asyncio
async def test_suspend_does_not_reconcile(tmp_vault, mock_config, runtime_setup,
                                          tmp_path, monkeypatch):
    """A blocking ESCALATE suspends — it must NOT write a request-outcome (the
    request is pending approval, not unresolved; reconciling now would
    double-count the terminal granted_* produced after resume)."""
    shadow, activity, scroll, tool = runtime_setup
    _redirect_snapshot(monkeypatch, tmp_path)
    _EscalateRecordingGovernor.reconcile_calls = []

    decisions = [
        {"action": "REQUEST_HARNESS", "kind": "tool",
         "spec": {"name": "x"}, "rationale": "need it", "fallback": "guess",
         "blocking": True},
        {"action": "COMPLETE", "summary": "unreached"},
    ]

    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.governor.Governor", _EscalateRecordingGovernor), \
         patch("systemu.interface.harness_review.surface_harness_request",
               lambda *a, **k: "decision_b11"), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(mock_config, tmp_vault)
        result = await runtime.execute(shadow, activity)

    assert result["status"] == "suspended_harness_escalation"
    assert _EscalateRecordingGovernor.reconcile_calls == [], \
        "suspend wrote a premature request-outcome (should defer to terminal)"
