"""Plan 0 Build 1 wiring — decision_audit recording + pull-decision field threading
into the shadow_runtime loop (observability only). Mirrors tests/test_shadow_runtime.py
fixtures.
"""
import pytest
from unittest.mock import patch, AsyncMock

from systemu.core.models import (
    Activity, Shadow, ShadowStatus, Tool, ToolStatus, ToolType, Scroll, Objective,
    HarnessVerdict, HarnessDecision, RiskBand,
)
from systemu.vault.vault import Vault
from systemu.runtime.decision_audit import read_iteration_decisions


@pytest.fixture
def tmp_vault(tmp_path):
    for sub in ["scrolls", "activities", "shadow_army", "skills", "tools/implementations",
                "evolutions", "notifications", "executions"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx in ["scrolls", "activities", "shadow_army", "skills", "tools", "evolutions"]:
        (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
    (tmp_path / "global_memory.jsonl").write_text("", encoding="utf-8")
    return Vault(str(tmp_path))


@pytest.fixture
def mock_config(tmp_path):
    from sharing_on.config import Config
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    cfg.intent_engine_enabled = True   # REQUEST_HARNESS path requires it
    return cfg


@pytest.fixture
def setup(tmp_vault):
    shadow = Shadow(id="sh1", name="T", description="d", system_prompt="p",
                    status=ShadowStatus.AWAKENED)
    tmp_vault.save_shadow(shadow)
    # execute() aborts pre-loop unless at least one deployed tool exists.
    tool = Tool(id="tool_1", name="test_tool", description="Test",
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
                enabled=True,
                implementation_path="vault/tools/implementations/test_tool.py")
    tmp_vault.save_tool(tool)
    scroll = Scroll(id="sc1", name="S", source_session_id="x",
                    raw_instructions_path="", narrative_md="",
                    objectives=[Objective(id=1, goal="do", success_criteria="done")])
    tmp_vault.save_scroll(scroll)
    activity = Activity(id="a1", name="A", scroll_id=scroll.id,
                        required_tool_ids=["tool_1"], required_skill_ids=[],
                        assigned_shadow_id=shadow.id)
    tmp_vault.save_activity(activity)
    return shadow, activity, scroll


@pytest.mark.asyncio
async def test_decision_audit_written_and_request_fields_threaded(tmp_vault, mock_config, setup):
    shadow, activity, scroll = setup
    decisions = [
        {"action": "REQUEST_HARNESS", "kind": "tool",
         "spec": {"name": "x", "description": "y"},
         "rationale": "need x", "fallback": "give up",
         "confidence": 0.8, "attempts_before": 2, "reasoning": "blocked"},
        {"action": "FAIL", "reason": "done testing"},
    ]
    captured = {}

    def _capture_arbitrate(self, request, *a, **k):
        captured["req"] = request
        return HarnessVerdict(request_id=request.request_id,
                              decision=HarnessDecision.DENY, risk_band=RiskBand.HIGH,
                              rationale="denied for test", alternatives=["adapt"])

    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.governor.Governor.arbitrate", _capture_arbitrate), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(mock_config, tmp_vault)
        result = await runtime.execute(shadow, activity)

    # Edit 3 — request fields threaded from the decision onto the HarnessRequest
    req = captured["req"]
    assert req.confidence == 0.8
    assert req.attempts_before_request == 2
    assert isinstance(req.provenance, dict)

    # Edit 2 — per-iteration decision audit written, with harness meta on the RH row
    rows = read_iteration_decisions(tmp_vault.root, result["execution_id"])
    actions = [r["action"] for r in rows]
    assert "REQUEST_HARNESS" in actions and "FAIL" in actions
    rh = next(r for r in rows if r["action"] == "REQUEST_HARNESS")
    assert rh["is_request_harness"] is True
    assert rh["harness_kind"] == "tool"
    assert rh["harness_confidence"] == 0.8
    assert rh["harness_attempts_before"] == 2


@pytest.mark.asyncio
async def test_confidence_out_of_range_does_not_break_the_loop(tmp_vault, mock_config, setup):
    """A hallucinated confidence must be clamped, never raise (would abort the run)."""
    shadow, activity, scroll = setup
    decisions = [
        {"action": "REQUEST_HARNESS", "kind": "tool", "spec": {},
         "rationale": "r", "confidence": 9.9, "attempts_before": "oops"},
        {"action": "FAIL", "reason": "x"},
    ]
    captured = {}

    def _cap(self, request, *a, **k):
        captured["req"] = request
        return HarnessVerdict(request_id=request.request_id,
                              decision=HarnessDecision.DENY, rationale="d")

    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.governor.Governor.arbitrate", _cap), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(mock_config, tmp_vault)
        await runtime.execute(shadow, activity)

    assert 0.0 <= captured["req"].confidence <= 1.0
    assert captured["req"].attempts_before_request == 0


def _grant(req):
    return HarnessVerdict(request_id=req.request_id, decision=HarnessDecision.GRANT,
                          risk_band=RiskBand.MEDIUM, rationale="ok", lease_id="lease1")


@pytest.mark.asyncio
async def test_subagent_grant_spawns_fleet_when_flag_on(tmp_vault, mock_config, setup):
    """Build 3 Task 3.6 — a GRANTed SUBAGENT request spawns the real parallel fleet
    (flag ON) and the agent receives the collated synthesis."""
    shadow, activity, scroll = setup
    mock_config.delegate_use_parallel = True
    decisions = [
        {"action": "REQUEST_HARNESS", "kind": "subagent",
         "spec": {"tasks": ["analyze A", "analyze B"]}, "rationale": "split"},
        {"action": "FAIL", "reason": "done"},
    ]
    seen = {}

    async def _fake_spawn(self, parent_shadow, parent_activity, tasks, **kw):
        seen["tasks"] = list(tasks)
        return {"synthesis": "Partial result: 1 of 2 completed. Done: A. Missing: B (failed).",
                "any_succeeded": True, "all_succeeded": False, "budget": {"tool_call_count": 3},
                "succeeded": ["A"], "failed": ["B"], "missing": ["B"], "children": []}

    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.governor.Governor.arbitrate", lambda self, req, *a, **k: _grant(req)), \
         patch("systemu.runtime.governor.Governor.materialise",
               lambda self, req, verdict, **k: {"materialised": True, "subagent": {"task": "analyze A"}, "lease_id": "lease1"}), \
         patch("systemu.runtime.subagent_fleet.SubagentFleet.spawn_children", _fake_spawn), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(mock_config, tmp_vault)
        await runtime.execute(shadow, activity)

    assert seen.get("tasks") == ["analyze A", "analyze B"]  # multi-task from spec


@pytest.mark.asyncio
async def test_subagent_grant_no_fleet_when_flag_off(tmp_vault, mock_config, setup):
    """Default off → no behavior change: the fleet is NOT spawned."""
    shadow, activity, scroll = setup
    mock_config.delegate_use_parallel = False
    decisions = [
        {"action": "REQUEST_HARNESS", "kind": "subagent", "spec": {"tasks": ["x"]}, "rationale": "r"},
        {"action": "FAIL", "reason": "done"},
    ]
    called = {"n": 0}

    async def _fake_spawn(self, *a, **k):
        called["n"] += 1
        return {"synthesis": "", "any_succeeded": False, "all_succeeded": False, "budget": {}}

    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.governor.Governor.arbitrate", lambda self, req, *a, **k: _grant(req)), \
         patch("systemu.runtime.governor.Governor.materialise",
               lambda self, req, verdict, **k: {"materialised": True, "subagent": {"task": "x"}, "lease_id": "l"}), \
         patch("systemu.runtime.subagent_fleet.SubagentFleet.spawn_children", _fake_spawn), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(mock_config, tmp_vault)
        await runtime.execute(shadow, activity)

    assert called["n"] == 0  # flag off → fleet not invoked (observation-only fallback)


@pytest.mark.asyncio
async def test_terminal_reconciliation_called_with_invoked_tools(tmp_vault, mock_config, setup):
    """Task 1.6 wiring — at terminal, write_outcome_reconciliation is invoked with
    the set of tools actually called during the run."""
    shadow, activity, scroll = setup
    decisions = [
        {"action": "TOOL_CALL", "tool_name": "test_tool", "parameters": {}},
        {"action": "FAIL", "reason": "done"},
    ]
    seen = {}

    def _cap_recon(self, execution_id, used_tool_names, *a, **k):
        seen["used"] = set(used_tool_names)
        seen["exec"] = execution_id
        return 0

    from systemu.runtime.tool_sandbox import ToolResult
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.shadow_runtime.ToolSandbox.execute_tool", new_callable=AsyncMock) as mexec, \
         patch("systemu.runtime.governor.Governor.write_outcome_reconciliation", _cap_recon), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        mexec.return_value = ToolResult(success=True, parsed={"out": "ok"})
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(mock_config, tmp_vault)
        await runtime.execute(shadow, activity)

    assert "test_tool" in seen.get("used", set())


@pytest.mark.asyncio
async def test_e2e_subagent_fleet_partial_failure_does_not_abort_parent(tmp_vault, mock_config, setup):
    """Task 3.7 — end-to-end: parent loop → SUBAGENT grant → REAL fleet → REAL
    collation, with only the CHILD execute mocked (discriminated by origin). One of
    three children fails; the fleet collates a partial result and the parent run
    completes normally (partial != total failure)."""
    shadow, activity, scroll = setup
    mock_config.delegate_use_parallel = True
    decisions = [
        {"action": "REQUEST_HARNESS", "kind": "subagent",
         "spec": {"tasks": ["a", "b", "c"]}, "rationale": "fan out"},
        {"action": "FAIL", "reason": "wrap up"},
    ]
    from systemu.runtime.shadow_runtime import ShadowRuntime
    real_execute = ShadowRuntime.execute
    child_calls = []

    async def smart_execute(self, shadow_, activity_, **kw):
        if str(kw.get("origin", "")).startswith("delegate-fleet-"):
            i = len(child_calls)
            child_calls.append(getattr(shadow_, "id", "?"))
            if i == 2:                      # the third child fails
                raise RuntimeError("child boom")
            return {"execution_id": f"child{i}", "status": "success",
                    "summary": f"child {i} done", "tool_calls": 1, "rounds": 1}
        return await real_execute(self, shadow_, activity_, **kw)   # parent: real

    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.governor.Governor.arbitrate", lambda self, req, *a, **k: _grant(req)), \
         patch("systemu.runtime.governor.Governor.materialise",
               lambda self, req, verdict, **k: {"materialised": True, "subagent": {"task": "a"}, "lease_id": "lz"}), \
         patch.object(ShadowRuntime, "execute", smart_execute), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        runtime = ShadowRuntime(mock_config, tmp_vault)
        result = await runtime.execute(shadow, activity)

    # all three children were really spawned (real fleet), the failing one didn't
    # abort the siblings, and the parent run completed to its terminal FAIL.
    assert len(child_calls) == 3
    assert result["status"] == "failure"


@pytest.mark.asyncio
async def test_harness_usage_slice_recorded_on_terminal(tmp_vault, mock_config, setup):
    """Task 1.7(c) wiring — a run that pulled the harness records the harness-usage
    slice at terminal with the run's success outcome (here: FAIL → success=False)."""
    shadow, activity, scroll = setup
    decisions = [
        {"action": "REQUEST_HARNESS", "kind": "tool", "spec": {},
         "rationale": "r", "confidence": 0.6, "attempts_before": 1},
        {"action": "FAIL", "reason": "done"},
    ]
    seen = {}

    def _cap(self, *, shadow_id, intent_hash, used_harness, success):
        seen.update(used_harness=used_harness, success=success, shadow_id=shadow_id)

    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.governor.Governor.arbitrate",
               lambda self, req, *a, **k: HarnessVerdict(
                   request_id=req.request_id, decision=HarnessDecision.DENY, rationale="d")), \
         patch("systemu.runtime.shadow_metrics.ShadowMetrics.note_harness_usage", _cap), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(mock_config, tmp_vault)
        await runtime.execute(shadow, activity)

    assert seen.get("used_harness") is True
    assert seen.get("success") is False        # FAIL terminal
    assert seen.get("shadow_id") == "sh1"


@pytest.mark.asyncio
async def test_audit_namespace_routes_action_audit(tmp_vault, mock_config, setup, tmp_path):
    """Item 1 — a ShadowRuntime constructed with audit_namespace routes its
    action-audit writes to that child namespace (fleet isolation)."""
    shadow, activity, scroll = setup
    ns = tmp_path / "child_ns"
    decisions = [
        {"action": "TOOL_CALL", "tool_name": "test_tool", "parameters": {}},
        {"action": "FAIL", "reason": "x"},
    ]
    captured = {}
    real_append = tmp_vault.append_action_audit

    def cap_append(entry, namespace_path=None):
        captured["ns"] = namespace_path
        return real_append(entry, namespace_path=namespace_path)

    from systemu.runtime.tool_sandbox import ToolResult
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.shadow_runtime.ToolSandbox.execute_tool", new_callable=AsyncMock) as me, \
         patch.object(tmp_vault, "append_action_audit", cap_append), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        me.return_value = ToolResult(success=True, parsed={"out": "ok"})
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(mock_config, tmp_vault, audit_namespace=ns)
        await runtime.execute(shadow, activity)

    assert captured.get("ns") == ns
