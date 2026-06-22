"""v0.8.15 — browser tools route to subprocess (sync-Playwright fix)."""
import pytest


class TestMustUseSubprocess:
    def test_browser_action_true(self):
        from systemu.runtime.tool_sandbox import _must_use_subprocess
        assert _must_use_subprocess("browser_action", []) is True

    def test_playwright_dep_true(self):
        from systemu.runtime.tool_sandbox import _must_use_subprocess
        assert _must_use_subprocess(None, ["playwright"]) is True
        assert _must_use_subprocess("api_call", ["requests", "selenium"]) is True

    def test_plain_tools_false(self):
        from systemu.runtime.tool_sandbox import _must_use_subprocess
        assert _must_use_subprocess("python_function", []) is False
        assert _must_use_subprocess("api_call", ["requests"]) is False
        assert _must_use_subprocess(None, None) is False


class TestSandboxRouting:
    @pytest.mark.asyncio
    async def test_browser_tool_skips_registry_uses_subprocess(self, tmp_path, monkeypatch):
        # ToolResult is defined in tool_sandbox itself (the backend returns it).
        from systemu.runtime.tool_sandbox import ToolResult, ToolSandbox
        impl = tmp_path / "web_navigate.py"
        impl.write_text("def run(**k):\n    return {'success': True}\n", encoding="utf-8")

        sb = ToolSandbox(vault_root=tmp_path, backend="local")
        registry_called = {"n": 0}
        class FakeReg:
            async def execute(self, *a, **k):
                registry_called["n"] += 1
                return {"success": True}
        sb.attach_registry(FakeReg())

        backend_called = {"n": 0}
        async def fake_backend_execute(path, params_json, *, timeout, extra_packages):
            backend_called["n"] += 1
            return ToolResult(success=True, parsed={"success": True})
        monkeypatch.setattr(sb._backend, "execute", fake_backend_execute)

        # browser_action -> subprocess (registry NOT used)
        await sb.execute_tool(str(impl), {}, tool_type="browser_action")
        assert registry_called["n"] == 0
        assert backend_called["n"] == 1

        # plain tool -> registry fast path
        await sb.execute_tool(str(impl), {}, tool_type="python_function")
        assert registry_called["n"] == 1


class TestShadowRuntimePassesToolType:
    def test_execute_tool_call_passes_tool_type(self):
        import inspect
        from systemu.runtime import shadow_runtime
        src = inspect.getsource(shadow_runtime)
        assert "tool_type=" in src and "execute_tool(" in src
        # the value form, not the enum repr — either `.value` directly or the
        # getattr(..., "value", ...) fallback form the plan prescribes:
        assert (
            ".value" in src
            or "tool_type=tool_obj.tool_type" in src
            or 'getattr(tool_obj.tool_type, "value"' in src
        )
