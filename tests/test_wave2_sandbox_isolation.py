"""Wave 2.2 — LLM-forged tool code runs OUT-OF-PROCESS by default.

The in-process ToolRegistry fast path (direct importlib call inside the
daemon) was the DEFAULT for all tools — an approved-but-malicious/buggy
forged tool had full daemon privileges (vault, env, network).  Now:

  * built-in tools (not ``forged_by_systemu``) keep the fast path;
  * forged tools route through the subprocess backend unless the operator
    explicitly marks them ``trusted_inprocess``.
"""
import pytest

from systemu.core.models import Tool, ToolStatus
from systemu.runtime.tool_sandbox import ToolSandbox, requires_subprocess_isolation


def _tool(*, forged: bool, trusted: bool = False) -> Tool:
    return Tool(
        id="tool_x", name="tool_x", description="t", tool_type="api_call",
        status=ToolStatus.DEPLOYED, enabled=True,
        forged_by_systemu=forged, trusted_inprocess=trusted,
    )


class TestIsolationPolicy:
    def test_forged_untrusted_is_isolated(self):
        assert requires_subprocess_isolation(_tool(forged=True)) is True

    def test_forged_trusted_keeps_fast_path(self):
        assert requires_subprocess_isolation(_tool(forged=True, trusted=True)) is False

    def test_builtin_keeps_fast_path(self):
        assert requires_subprocess_isolation(_tool(forged=False)) is False

    def test_default_tool_field_is_untrusted(self):
        t = Tool(id="t", name="t", description="d", tool_type="api_call")
        assert t.trusted_inprocess is False

    def test_none_tool_is_isolated_defensively(self):
        # No Tool context at all → isolate (cannot prove it's a built-in).
        assert requires_subprocess_isolation(None) is True


class _RegistryMustNotBeUsed:
    async def execute(self, *a, **k):
        raise AssertionError("fast path must not run under force_subprocess")


class _RecordingBackend:
    def __init__(self):
        self.calls = []

    @property
    def name(self):
        return "recording"

    async def execute(self, impl_path, params_json, *, timeout, extra_packages):
        self.calls.append(str(impl_path))
        from systemu.runtime.tool_sandbox import ToolResult
        return ToolResult(success=True, parsed={"via": "subprocess"})


@pytest.mark.asyncio
async def test_force_subprocess_skips_registry(tmp_path):
    impl = tmp_path / "vault" / "tools" / "implementations" / "tool_x.py"
    impl.parent.mkdir(parents=True)
    impl.write_text("print('{}')", encoding="utf-8")

    sandbox = ToolSandbox(tmp_path / "vault", registry=_RegistryMustNotBeUsed())
    sandbox._backend = _RecordingBackend()

    result = await sandbox.execute_tool(
        str(impl), {}, force_subprocess=True,
    )
    assert result.success and result.parsed.get("via") == "subprocess"
    assert sandbox._backend.calls, "subprocess backend must have executed"


def test_shadow_runtime_passes_isolation_flag():
    import inspect
    from systemu.runtime import shadow_runtime
    src = inspect.getsource(shadow_runtime)
    assert "force_subprocess" in src and "requires_subprocess_isolation" in src
