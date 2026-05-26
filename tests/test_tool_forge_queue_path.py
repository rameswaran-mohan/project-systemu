"""E2E shape test: forge_tool through the queue-mode notify_user path (v0.8.0)."""
from unittest.mock import MagicMock, patch
import pytest


def test_first_forge_call_raises_pending_decision(tmp_path, monkeypatch):
    """In queue mode with no resolved decision, forge_tool should propagate
    PendingOperatorDecision (the caller catches and exits cleanly)."""
    monkeypatch.setenv("SYSTEMU_DECISION_QUEUE", "true")
    monkeypatch.delenv("SYSTEMU_HEADLESS", raising=False)
    monkeypatch.delenv("SYSTEMU_NON_INTERACTIVE", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    from systemu.vault.vault import Vault
    from systemu.approval.exceptions import PendingOperatorDecision
    from systemu.core.models import Tool, ToolType, ToolStatus
    from systemu.core.models import Scroll as ScrollModel
    from systemu.interface import notifications as N

    vault = Vault(str(tmp_path))
    monkeypatch.setattr(N, "_vault", vault)
    monkeypatch.setattr(N, "_decision_queue_instance", None, raising=False)

    tool = Tool(
        id="tool_x",
        name="test_tool",
        description="x",
        tool_type=ToolType.PYTHON_FUNCTION,
        dependencies=[],
        status=ToolStatus.PROPOSED,
        forged_by_systemu=True,
    )
    scroll = ScrollModel(
        id="scroll_x", name="x", source_session_id="x",
        raw_instructions_path="", narrative_md="x",
    )
    fake_config = MagicMock()

    from systemu.pipelines.tool_forge import forge_tool

    with pytest.raises(PendingOperatorDecision) as exc_info:
        forge_tool(tool, scroll, fake_config, vault)

    assert exc_info.value.dedup_key == "tool_forge:tool_x"

    # The decision should be persisted in the vault
    from systemu.approval.decision_queue import OperatorDecisionQueue
    queue = OperatorDecisionQueue(vault)
    pending = queue.list_pending()
    assert len(pending) == 1
    assert pending[0].dedup_key == "tool_forge:tool_x"


def test_second_forge_call_returns_choice_after_resolve(tmp_path, monkeypatch):
    """After the operator resolves the decision, a re-attempt of forge_tool
    should pass the gate and proceed to code generation."""
    monkeypatch.setenv("SYSTEMU_DECISION_QUEUE", "true")
    monkeypatch.delenv("SYSTEMU_HEADLESS", raising=False)
    monkeypatch.delenv("SYSTEMU_NON_INTERACTIVE", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    from systemu.vault.vault import Vault
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.core.models import Tool, ToolType, ToolStatus
    from systemu.core.models import Scroll as ScrollModel
    from systemu.interface import notifications as N

    vault = Vault(str(tmp_path))
    monkeypatch.setattr(N, "_vault", vault)
    monkeypatch.setattr(N, "_decision_queue_instance", None, raising=False)

    # Pre-resolve a decision with the matching dedup_key
    queue = OperatorDecisionQueue(vault)
    dec_id = queue.post(
        title="Forge?", body="x",
        options=["Skip", "Forge"],
        dedup_key="tool_forge:tool_y",
    )
    queue.resolve(dec_id, choice="Forge")

    tool = Tool(
        id="tool_y", name="test_y", description="x",
        tool_type=ToolType.PYTHON_FUNCTION,
        dependencies=[],
        status=ToolStatus.PROPOSED,
        forged_by_systemu=True,
    )
    scroll = ScrollModel(
        id="scroll_y", name="y", source_session_id="y",
        raw_instructions_path="", narrative_md="y",
    )
    fake_config = MagicMock()

    # Patch _generate_and_save_code to a no-op so we can assert it was called.
    with patch(
        "systemu.pipelines.tool_forge._generate_and_save_code",
        return_value=tool,
    ) as gen:
        from systemu.pipelines.tool_forge import forge_tool
        result = forge_tool(tool, scroll, fake_config, vault)

    assert result is tool
    gen.assert_called_once()
