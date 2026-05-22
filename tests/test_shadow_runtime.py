import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from systemu.core.models import Activity, Shadow, ShadowStatus, Tool, ToolStatus, ToolType, Scroll, Objective
from systemu.core.utils import generate_id
from systemu.vault.vault import Vault
from systemu.runtime.tool_sandbox import ToolResult

@pytest.fixture
def tmp_vault(tmp_path):
    for sub in ["scrolls", "activities", "shadow_army", "skills", "tools/implementations",
                "evolutions", "notifications", "executions"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx_dir in ["scrolls", "activities", "shadow_army", "skills", "tools", "evolutions"]:
        (tmp_path / idx_dir / "index.json").write_text("[]", encoding="utf-8")
    (tmp_path / "global_memory.jsonl").write_text("", encoding="utf-8")
    return Vault(str(tmp_path))

@pytest.fixture
def mock_config(tmp_path):
    """Real Config dataclass — MagicMock-based config breaks json.dumps inside
    shadow_runtime when it tries to serialise context into the LLM prompt."""
    from sharing_on.config import Config
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    return cfg

@pytest.fixture
def runtime_setup(tmp_vault, mock_config):
    shadow = Shadow(id="shadow_1", name="Test Shadow", description="Test", system_prompt="Test", status=ShadowStatus.AWAKENED)
    tmp_vault.save_shadow(shadow)

    tool = Tool(
        id="tool_1", name="test_tool", description="Test",
        tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
        enabled=True, implementation_path="vault/tools/implementations/test_tool.py"
    )
    tmp_vault.save_tool(tool)

    scroll = Scroll(
        id="scroll_1", name="Test Scroll", source_session_id="s1",
        raw_instructions_path="", narrative_md="",
        objectives=[Objective(id=1, goal="Do something", success_criteria="Done")]
    )
    tmp_vault.save_scroll(scroll)

    activity = Activity(
        id="act_1", name="Test Activity", scroll_id=scroll.id,
        required_tool_ids=["tool_1"], required_skill_ids=[],
        assigned_shadow_id=shadow.id
    )
    tmp_vault.save_activity(activity)

    return shadow, activity, scroll, tool

@pytest.mark.asyncio
async def test_execute_single_objective(tmp_vault, mock_config, runtime_setup):
    shadow, activity, scroll, tool = runtime_setup

    mock_llm_decisions = [
        {"action": "TOOL_CALL", "tool_name": "test_tool", "parameters": {}, "completes_objective": 1},
        {"action": "COMPLETE", "summary": "Done."}
    ]

    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=mock_llm_decisions):
        with patch("systemu.runtime.shadow_runtime.ToolSandbox.execute_tool", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = ToolResult(success=True, parsed={"out": "ok"})
            with patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
                from systemu.runtime.shadow_runtime import ShadowRuntime
                runtime = ShadowRuntime(mock_config, tmp_vault)
                
                result = await runtime.execute(shadow, activity)

                assert result["status"] == "success"
                assert mock_exec.call_count == 1
                
                updated_shadow = tmp_vault.get_shadow(shadow.id)
                assert len(updated_shadow.execution_log) == 1
                assert updated_shadow.execution_log[0]["status"] == "success"

@pytest.mark.asyncio
async def test_handles_tool_error(tmp_vault, mock_config, runtime_setup):
    shadow, activity, scroll, tool = runtime_setup

    mock_llm_decisions = [
        {"action": "TOOL_CALL", "tool_name": "test_tool", "parameters": {}},
        {"action": "THINK", "thought": "Tool failed, I give up."},
        {"action": "FAIL", "reason": "Tool error"}
    ]

    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=mock_llm_decisions):
        with patch("systemu.runtime.shadow_runtime.ToolSandbox.execute_tool", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = ToolResult(success=False, error="File not found")
            with patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
                from systemu.runtime.shadow_runtime import ShadowRuntime
                runtime = ShadowRuntime(mock_config, tmp_vault)
                
                result = await runtime.execute(shadow, activity)

                assert result["status"] == "failure"
                assert mock_exec.call_count == 1
                
                updated_shadow = tmp_vault.get_shadow(shadow.id)
                assert updated_shadow.execution_log[0]["status"] == "failure"
                assert "Tool error" in updated_shadow.execution_log[0]["summary"]

@pytest.mark.asyncio
async def test_max_iterations_reached(tmp_vault, mock_config, runtime_setup):
    shadow, activity, scroll, tool = runtime_setup

    mock_llm_decisions = [{"action": "THINK", "thought": "Thinking loop..."}] * 51

    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=mock_llm_decisions):
        with patch("systemu.runtime.shadow_runtime.MAX_ITERATIONS", 5):
            from systemu.runtime.shadow_runtime import ShadowRuntime
            runtime = ShadowRuntime(mock_config, tmp_vault)
            
            result = await runtime.execute(shadow, activity)

            assert result["status"] == "partial"
            assert "MaxIterationsExceeded" in result.get("error", "")
            
            updated_shadow = tmp_vault.get_shadow(shadow.id)
            assert updated_shadow.execution_log[0]["status"] == "partial"
