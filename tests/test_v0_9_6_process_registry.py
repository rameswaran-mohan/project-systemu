"""v0.9.6 L7 process_registry tests."""
import time
import pytest


class TestProcessRegistry:
    def test_register_and_list(self):
        from systemu.runtime.process_registry import (
            register_process, list_processes, clear_all,
        )
        clear_all()
        pid = register_process(name="curl_test", command="curl http://example.com", pid=12345)
        assert isinstance(pid, str)
        out = list_processes()
        assert len(out) == 1
        assert out[0]["name"] == "curl_test"
        assert out[0]["pid"] == 12345

    def test_check_process_returns_none_for_unknown(self):
        from systemu.runtime.process_registry import check_process, clear_all
        clear_all()
        assert check_process("nonexistent_id") is None

    def test_check_process_returns_state(self):
        from systemu.runtime.process_registry import (
            register_process, check_process, clear_all,
        )
        clear_all()
        pid = register_process(name="test", command="echo", pid=99999)
        info = check_process(pid)
        assert info is not None
        assert info["name"] == "test"
        assert "registered_at" in info
        assert "status" in info

    def test_mark_process_done(self):
        from systemu.runtime.process_registry import (
            register_process, mark_done, check_process, clear_all,
        )
        clear_all()
        pid = register_process(name="t", command="ls", pid=12345)
        mark_done(pid, exit_code=0, stdout="output", stderr="")
        info = check_process(pid)
        assert info["status"] == "completed"
        assert info["exit_code"] == 0
        assert "output" in info.get("stdout", "")

    def test_clear_completed_keeps_running(self):
        from systemu.runtime.process_registry import (
            register_process, mark_done, list_processes, clear_completed,
            clear_all,
        )
        clear_all()
        p1 = register_process(name="done", command="x", pid=1)
        mark_done(p1, exit_code=0)
        p2 = register_process(name="running", command="y", pid=2)
        clear_completed()
        remaining = list_processes()
        assert len(remaining) == 1
        assert remaining[0]["name"] == "running"


class TestProcessRegistryLlmTool:
    def test_process_list_tool_registered(self):
        from systemu.runtime.tool_registry_v2 import registry as singleton
        import systemu.runtime.tools.process_tools  # noqa: F401 — triggers registration
        entry = singleton.get("process_list")
        assert entry is not None
        assert entry.toolset == "process"

    def test_process_check_tool_registered(self):
        from systemu.runtime.tool_registry_v2 import registry as singleton
        import systemu.runtime.tools.process_tools  # noqa: F401
        entry = singleton.get("process_check")
        assert entry is not None


class TestProcessToolsBootDiscovery:
    """v0.9.6 regression guard: the process tools must be registered by the
    SAME boot-time discovery path production uses, not just by importing the
    module directly.

    Before this guard existed, process_list/process_check registered in
    systemu.runtime.process_registry — which lives OUTSIDE the
    systemu.runtime.tools package that _discover_v2_tools() AST-scans — so the
    tools were invisible to the LLM in production despite green unit tests.
    """

    def test_boot_discovery_registers_process_tools(self):
        # Exercise the EXACT production discovery call (shadow_runtime.py:218 /
        # _discover_v2_tools both scan this package).
        from systemu.runtime.tool_registry_v2 import registry as singleton
        singleton.discover_modules("systemu.runtime.tools")
        assert singleton.get("process_list") is not None, (
            "process_list must be registered by boot discovery of "
            "systemu.runtime.tools — move its register() call into a module "
            "under that package (process_tools.py)."
        )
        assert singleton.get("process_check") is not None
        assert singleton.get("process_list").toolset == "process"

    def test_shadow_runtime_discovery_includes_process_tools(self):
        """Use shadow_runtime's own discovery entrypoint to prove the wiring
        holds through the production code path, not just a raw registry call."""
        import importlib
        sr = importlib.import_module("systemu.runtime.shadow_runtime")
        # _V2_DISCOVERED may already be True from a prior import; force a fresh
        # discovery against the real package to assert the tools land.
        sr._V2_DISCOVERED = False
        sr._discover_v2_tools()
        from systemu.runtime.tool_registry_v2 import registry as singleton
        assert singleton.get("process_list") is not None
        assert singleton.get("process_check") is not None

    def test_process_list_handler_returns_success(self):
        from systemu.runtime.process_registry import (
            _process_list_handler, register_process, clear_all,
        )
        clear_all()
        register_process(name="t", command="x", pid=1)
        result = _process_list_handler()
        assert result["success"] is True
        assert isinstance(result["processes"], list)
        assert len(result["processes"]) == 1
