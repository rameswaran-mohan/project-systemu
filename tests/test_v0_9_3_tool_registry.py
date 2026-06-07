"""v0.9.3 tool_registry_v2 tests — Hermes-style code-side registry."""
import time
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from sharing_on.config import Config
from systemu.runtime.tool_registry_v2 import ToolRegistry, ToolEntry


class TestRegisterAndGet:
    def test_register_and_get(self):
        r = ToolRegistry()
        def handler(**kw): return {"ok": True}
        r.register(
            name="my_tool", toolset="test",
            schema={"type": "object"}, handler=handler,
            description="t",
        )
        e = r.get("my_tool")
        assert e is not None
        assert e.name == "my_tool"
        assert e.toolset == "test"
        assert e.handler is handler

    def test_get_returns_none_for_unknown(self):
        r = ToolRegistry()
        assert r.get("nope") is None

    def test_register_twice_overwrites(self):
        """Re-registering by name updates the entry (last write wins)."""
        r = ToolRegistry()
        def h1(**kw): return 1
        def h2(**kw): return 2
        r.register(name="t", toolset="x", schema={}, handler=h1)
        r.register(name="t", toolset="x", schema={}, handler=h2)
        assert r.get("t").handler is h2


class TestListByToolset:
    def test_list_returns_all(self):
        r = ToolRegistry()
        r.register(name="a", toolset="file", schema={}, handler=lambda **k: None)
        r.register(name="b", toolset="web", schema={}, handler=lambda **k: None)
        r.register(name="c", toolset="file", schema={}, handler=lambda **k: None)
        names = sorted([e.name for e in r.list()])
        assert names == ["a", "b", "c"]

    def test_list_by_toolset_filters(self):
        r = ToolRegistry()
        r.register(name="a", toolset="file", schema={}, handler=lambda **k: None)
        r.register(name="b", toolset="web", schema={}, handler=lambda **k: None)
        file_only = r.list_by_toolset("file")
        assert [e.name for e in file_only] == ["a"]


class TestCheckFnAvailability:
    def test_available_true_when_no_check_fn(self):
        r = ToolRegistry()
        r.register(name="t", toolset="x", schema={}, handler=lambda **k: None)
        cfg = Config()
        assert r.available("t", cfg) is True

    def test_available_true_when_check_fn_passes(self):
        r = ToolRegistry()
        r.register(name="t", toolset="x", schema={}, handler=lambda **k: None,
                   check_fn=lambda: True)
        cfg = Config()
        assert r.available("t", cfg) is True

    def test_available_false_when_check_fn_fails(self):
        r = ToolRegistry()
        r.register(name="t", toolset="x", schema={}, handler=lambda **k: None,
                   check_fn=lambda: False)
        cfg = Config()
        assert r.available("t", cfg) is False

    def test_available_false_when_check_fn_raises(self):
        r = ToolRegistry()
        def boom(): raise RuntimeError("env not ready")
        r.register(name="t", toolset="x", schema={}, handler=lambda **k: None,
                   check_fn=boom)
        cfg = Config()
        assert r.available("t", cfg) is False

    def test_available_unknown_tool_is_false(self):
        r = ToolRegistry()
        assert r.available("nope", Config()) is False


class TestCheckFnCache:
    def test_check_fn_result_cached(self):
        """check_fn is only called once within the TTL window."""
        r = ToolRegistry()
        call_count = {"n": 0}
        def cf():
            call_count["n"] += 1
            return True
        r.register(name="t", toolset="x", schema={}, handler=lambda **k: None,
                   check_fn=cf)
        cfg = Config()
        cfg.check_fn_cache_ttl_seconds = 30
        r.available("t", cfg)
        r.available("t", cfg)
        r.available("t", cfg)
        assert call_count["n"] == 1

    def test_check_fn_recomputed_after_ttl(self, monkeypatch):
        r = ToolRegistry()
        call_count = {"n": 0}
        def cf():
            call_count["n"] += 1
            return True
        r.register(name="t", toolset="x", schema={}, handler=lambda **k: None,
                   check_fn=cf)
        cfg = Config()
        cfg.check_fn_cache_ttl_seconds = 1
        # First call populates cache
        r.available("t", cfg)
        # Advance monotonic clock by 2s
        original_monotonic = time.monotonic
        offset = {"v": 2.0}
        monkeypatch.setattr(
            "systemu.runtime.tool_registry_v2.time.monotonic",
            lambda: original_monotonic() + offset["v"])
        r.available("t", cfg)
        assert call_count["n"] == 2

    def test_invalidate_cache_clears(self):
        r = ToolRegistry()
        call_count = {"n": 0}
        def cf():
            call_count["n"] += 1
            return True
        r.register(name="t", toolset="x", schema={}, handler=lambda **k: None,
                   check_fn=cf)
        cfg = Config()
        r.available("t", cfg)
        r.invalidate_check_fn_cache()
        r.available("t", cfg)
        assert call_count["n"] == 2


class TestAstDiscovery:
    def test_discover_finds_register_calls(self, tmp_path, monkeypatch):
        """Drop a Python file into a discoverable directory; ensure the
        AST scan picks it up and importing it registers the tool."""
        pkg_dir = tmp_path / "fake_tools"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "demo.py").write_text(
            "from systemu.runtime.tool_registry_v2 import registry\n"
            "def _demo(**kw): return {'ok': True}\n"
            "registry.register(name='demo_tool', toolset='demo', "
            "schema={'type':'object'}, handler=_demo)\n"
        )

        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            # Use the singleton registry — that's what the demo.py imports.
            from systemu.runtime.tool_registry_v2 import registry as singleton
            singleton.discover_modules("fake_tools")
            assert singleton.get("demo_tool") is not None
            assert singleton.get("demo_tool").toolset == "demo"
        finally:
            sys.path.remove(str(tmp_path))
            # Clean up the singleton so the test doesn't pollute later tests
            singleton._tools.pop("demo_tool", None)
            singleton.invalidate_check_fn_cache()


class TestSingleton:
    def test_singleton_exists(self):
        from systemu.runtime.tool_registry_v2 import registry as singleton
        assert isinstance(singleton, ToolRegistry)


class TestFileTools:
    def test_module_load_registers_tools(self):
        """Importing file_tools.py registers 3 tools into the singleton."""
        from systemu.runtime.tool_registry_v2 import registry as singleton
        # Reset any prior state on the singleton
        for n in ("read_file", "write_file", "search_files"):
            singleton._tools.pop(n, None)
        # Importing triggers module-level register() calls
        import systemu.runtime.tools.file_tools  # noqa: F401
        assert singleton.get("read_file") is not None
        assert singleton.get("write_file") is not None
        assert singleton.get("search_files") is not None

    def test_all_three_are_in_file_toolset(self):
        from systemu.runtime.tool_registry_v2 import registry as singleton
        import systemu.runtime.tools.file_tools  # noqa: F401
        for n in ("read_file", "write_file", "search_files"):
            entry = singleton.get(n)
            assert entry is not None
            assert entry.toolset == "file"

    def test_max_result_size_chars_set(self):
        """Read tools have a sane output cap so a huge file doesn't blow context."""
        from systemu.runtime.tool_registry_v2 import registry as singleton
        import systemu.runtime.tools.file_tools  # noqa: F401
        assert singleton.get("read_file").max_result_size_chars == 100_000

    def test_read_file_works(self, tmp_path):
        from systemu.runtime.tools.file_tools import read_file_handler
        f = tmp_path / "hello.txt"
        f.write_text("hello world")
        out = read_file_handler(path="hello.txt", _root=str(tmp_path))
        assert out["success"] is True
        assert out["content"] == "hello world"

    def test_read_file_rejects_traversal(self, tmp_path):
        from systemu.runtime.tools.file_tools import read_file_handler
        out = read_file_handler(path="../../etc/passwd", _root=str(tmp_path))
        assert out["success"] is False
        assert "escapes" in out["error"].lower() or "security" in out["error"].lower()

    def test_write_file_creates(self, tmp_path):
        from systemu.runtime.tools.file_tools import write_file_handler
        out = write_file_handler(path="new.txt", content="hi", _root=str(tmp_path))
        assert out["success"] is True
        assert (tmp_path / "new.txt").read_text() == "hi"

    def test_search_files_glob(self, tmp_path):
        from systemu.runtime.tools.file_tools import search_files_handler
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.py").write_text("")
        (tmp_path / "c.md").write_text("")
        out = search_files_handler(pattern="*.py", root=".", _root=str(tmp_path))
        assert out["success"] is True
        files = sorted([Path(p).name for p in out["files"]])
        assert files == ["a.py", "b.py"]


class TestPerContextWhitelists:
    def test_verifier_fork_whitelist(self):
        from systemu.runtime.tool_registry_v2 import ToolRegistry
        r = ToolRegistry()
        out = r.whitelist_for_context("verifier_fork")
        assert isinstance(out, set)
        # Read-only tools only
        assert "read_file" in out
        # Action tools must NOT be in this whitelist
        assert "write_file" not in out

    def test_curator_whitelist(self):
        from systemu.runtime.tool_registry_v2 import ToolRegistry
        r = ToolRegistry()
        out = r.whitelist_for_context("curator")
        assert "skill_list" in out or "skill.list" in out
        # No file write
        assert "write_file" not in out

    def test_fact_extractor_whitelist(self):
        from systemu.runtime.tool_registry_v2 import ToolRegistry
        r = ToolRegistry()
        out = r.whitelist_for_context("fact_extractor")
        # write_user_fact is the only allowed write
        assert any("user_fact" in n for n in out) or "write_user_fact" in out

    def test_delegate_child_whitelist_excludes_delegate(self):
        from systemu.runtime.tool_registry_v2 import ToolRegistry
        r = ToolRegistry()
        out = r.whitelist_for_context("delegate_child")
        # No recursion — delegate must NOT be in the child whitelist
        assert "delegate" not in out
        assert "spawn_subagent" not in out

    def test_main_returns_all_registered(self):
        from systemu.runtime.tool_registry_v2 import ToolRegistry
        r = ToolRegistry()
        r.register(name="x", toolset="t", schema={}, handler=lambda **k: None)
        out = r.whitelist_for_context("main")
        assert "x" in out

    def test_unknown_context_raises(self):
        from systemu.runtime.tool_registry_v2 import ToolRegistry
        r = ToolRegistry()
        with pytest.raises(ValueError):
            r.whitelist_for_context("totally_made_up_context")


import asyncio


class TestSandboxV2Dispatch:
    def test_v2_registered_tool_executes_via_v2(self, tmp_path):
        """A tool registered in v2 registry executes via v2 handler, NOT v1."""
        from systemu.runtime.tool_registry_v2 import registry as singleton
        from systemu.runtime.tool_sandbox import ToolSandbox
        # Register a v2-only tool with a unique name
        called = []
        def my_handler(**kw):
            called.append(kw)
            return {"success": True, "value": "from_v2"}
        singleton.register(
            name="v2_only_tool", toolset="test",
            schema={"type": "object"}, handler=my_handler,
        )
        try:
            from sharing_on.config import Config
            sandbox = ToolSandbox(vault=None, config=Config())
            result = asyncio.run(sandbox.execute("v2_only_tool", {"x": 1}))
            assert result["success"] is True
            assert result["value"] == "from_v2"
            assert len(called) == 1
        finally:
            singleton._tools.pop("v2_only_tool", None)

    def test_v1_fallback_when_not_in_v2(self, tmp_path, monkeypatch):
        """Tool not in v2 registry falls back to v1 vault-based path."""
        from systemu.runtime.tool_registry_v2 import registry as singleton
        from systemu.runtime.tool_sandbox import ToolSandbox
        # Ensure the name is NOT in v2 registry
        singleton._tools.pop("legacy_vault_tool", None)
        # Monkeypatch the v1 dispatch site — execute_tool inside ToolSandbox.execute
        # — to return a known sentinel. The simplest hook: replace the find_tool_by_name
        # method on a fake vault so v1 dispatch returns "not found" and we get the
        # standard error shape from the v1 path.
        from sharing_on.config import Config
        sandbox = ToolSandbox(vault=None, config=Config())
        result = asyncio.run(sandbox.execute("legacy_vault_tool", {}))
        # v1 path: tool not found in vault → returns success=False with error
        assert result.get("success") is False

    def test_v2_check_fn_unavailable_falls_back_to_v1(self, monkeypatch):
        """When v2 entry exists but check_fn says unavailable, fall back to v1."""
        from systemu.runtime.tool_registry_v2 import registry as singleton
        from systemu.runtime.tool_sandbox import ToolSandbox
        singleton.register(
            name="needs_docker", toolset="test",
            schema={}, handler=lambda **kw: {"success": True},
            check_fn=lambda: False,  # always unavailable
        )
        try:
            from sharing_on.config import Config
            sandbox = ToolSandbox(vault=None, config=Config())
            result = asyncio.run(sandbox.execute("needs_docker", {}))
            # v2 says unavailable → fall back to v1 (which can't find it either,
            # so we get a failure). Key thing: v2 handler was NOT called.
            assert result.get("success") is False
        finally:
            singleton._tools.pop("needs_docker", None)

    def test_v2_handler_exception_returns_failure(self):
        """Handler exception is captured into a failure result, not raised."""
        from systemu.runtime.tool_registry_v2 import registry as singleton
        from systemu.runtime.tool_sandbox import ToolSandbox
        def boom(**kw):
            raise RuntimeError("kaboom")
        singleton.register(
            name="boom_tool", toolset="test",
            schema={}, handler=boom,
        )
        try:
            from sharing_on.config import Config
            sandbox = ToolSandbox(vault=None, config=Config())
            result = asyncio.run(sandbox.execute("boom_tool", {}))
            assert result["success"] is False
            assert "kaboom" in result.get("error", "")
        finally:
            singleton._tools.pop("boom_tool", None)


class TestShadowRuntimeDiscoveryAndWhitelist:
    def test_discover_populates_singleton_at_runtime_init(self, monkeypatch):
        """Calling _discover_v2_tools() populates the singleton."""
        import importlib
        import sys
        import systemu.runtime.shadow_runtime as _sr_mod
        from systemu.runtime.tool_registry_v2 import registry as singleton

        # Clean state — remove file toolset entries and reset the idempotency flag
        # so _discover_v2_tools() will re-run discover_modules().
        for n in list(singleton._tools.keys()):
            if singleton._tools[n].toolset == "file":
                singleton._tools.pop(n, None)
        monkeypatch.setattr(_sr_mod, "_V2_DISCOVERED", False)

        # Force-reload file_tools so it re-executes registry.register() calls
        # (importlib.import_module is idempotent for already-cached modules).
        ft_key = "systemu.runtime.tools.file_tools"
        if ft_key in sys.modules:
            importlib.reload(sys.modules[ft_key])

        from systemu.runtime.shadow_runtime import _discover_v2_tools
        _discover_v2_tools()
        # file_tools.py should now be loaded
        assert singleton.get("read_file") is not None
        assert singleton.get("write_file") is not None

    def test_verifier_whitelist_via_registry(self):
        """Verifier whitelist comes from the registry, not a hard-coded set."""
        from systemu.runtime.shadow_runtime import _resolve_tool_whitelist
        wl = _resolve_tool_whitelist("verifier_fork")
        assert isinstance(wl, set)
        assert "read_file" in wl
        # Action tools must NOT be in verifier whitelist
        assert "write_file" not in wl

    def test_main_context_returns_all_registered(self):
        from systemu.runtime.shadow_runtime import _resolve_tool_whitelist
        from systemu.runtime.tool_registry_v2 import registry as singleton
        singleton.register(
            name="some_main_tool", toolset="x",
            schema={}, handler=lambda **k: None,
        )
        try:
            wl = _resolve_tool_whitelist("main")
            assert "some_main_tool" in wl
        finally:
            singleton._tools.pop("some_main_tool", None)

    def test_unknown_context_returns_empty(self):
        """Unknown context returns empty set (defensive; runtime callers
        treat empty whitelist as 'allow nothing')."""
        from systemu.runtime.shadow_runtime import _resolve_tool_whitelist
        wl = _resolve_tool_whitelist("brand_new_context")
        assert wl == set()
