"""Bug #19: tool dry-run must pass install_mode + approval store to sandbox.

Without this, the PROMPT mode fail-closes with "no approval store" warning
and the tool's pip dependencies are never installed, so dry-run fails with
DRY_RUN_FAILED_BUG even though everything is actually approved.
"""
from unittest.mock import MagicMock, patch
import pytest


def test_dryrun_execute_constructs_sandbox_with_approvals():
    """The dry-run _execute function must thread install_mode + approvals
    into ToolSandbox so the dep installer can satisfy package requirements."""
    from systemu.pipelines import tool_dry_run

    fake_tool = MagicMock()
    fake_tool.implementation_path = "/tmp/fake_tool.py"
    fake_tool.dependencies = ["requests"]
    fake_tool.name = "fake_tool"

    fake_vault = MagicMock()
    fake_config = MagicMock()
    fake_config.vault_dir = "/tmp/fake_vault"
    fake_config.tool_dep_install_mode = "auto"
    fake_config.systemu_mode = "local"
    fake_config.docker_tool_timeout = 30

    captured = {}

    class FakeSandbox:
        def __init__(self, *, vault_root, default_timeout, install_mode=None, approvals=None):
            captured["install_mode"] = install_mode
            captured["approvals"] = approvals

        async def execute_tool(self, *a, **kw):
            class _R:
                def to_dict(self):
                    return {"success": True}
            return _R()

    with patch.object(tool_dry_run, "ToolSandbox", FakeSandbox, create=True):
        # Patch the import inside _execute by patching the module attribute
        with patch("systemu.runtime.tool_sandbox.ToolSandbox", FakeSandbox):
            result = tool_dry_run._execute(
                fake_tool, {"k": "v"}, vault=fake_vault, config=fake_config,
            )

    # Whatever resolve_install_mode returned, it must NOT be None — the
    # dep installer needs a real mode to do its job
    assert captured.get("install_mode") is not None, (
        "Bug #19: ToolSandbox constructed with install_mode=None — dep installer "
        "will warn 'PROMPT mode with no approval store' and fail."
    )
    assert captured.get("approvals") is not None, (
        "Bug #19: ToolSandbox constructed with approvals=None — dep installer "
        "fail-closes in PROMPT mode."
    )
