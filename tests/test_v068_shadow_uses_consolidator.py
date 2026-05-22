from unittest.mock import MagicMock, patch


def test_build_memory_context_uses_consolidator():
    from systemu.runtime.shadow_runtime import ShadowRuntime

    shadow = MagicMock()
    shadow.execution_log = [{"status": "failed", "tool": "x", "reason": "boom"}]
    vault = MagicMock()

    rt = ShadowRuntime.__new__(ShadowRuntime)
    rt.shadow = shadow
    rt.vault = vault

    with patch(
        "systemu.runtime.memory_consolidator.MemoryConsolidator.consolidate",
        return_value="CONSOLIDATED_VIEW",
    ) as mock_c:
        ctx = rt._build_memory_context_for_prompt()
    mock_c.assert_called_once()
    assert "CONSOLIDATED_VIEW" in ctx


def test_build_memory_context_empty_log_returns_empty():
    from systemu.runtime.shadow_runtime import ShadowRuntime

    shadow = MagicMock()
    shadow.execution_log = []
    vault = MagicMock()

    rt = ShadowRuntime.__new__(ShadowRuntime)
    rt.shadow = shadow
    rt.vault = vault

    ctx = rt._build_memory_context_for_prompt()
    assert ctx == ""
