"""v0.9.5 T3 — MCP wrapper tests (no live server)."""
import os
from unittest.mock import patch
import pytest


class TestConfigMcpFields:
    def test_default_server_urls_empty(self, monkeypatch):
        monkeypatch.delenv("SYSTEMU_MCP_SERVER_URLS", raising=False)
        from sharing_on.config import Config
        cfg = Config()
        assert cfg.mcp_server_urls == ""

    def test_env_override(self):
        env = {"SYSTEMU_MCP_SERVER_URLS": "http://localhost:8080,http://remote:9000"}
        with patch.dict(os.environ, env, clear=False):
            from sharing_on.config import Config
            cfg = Config.from_env()
        assert "localhost:8080" in cfg.mcp_server_urls
        assert "remote:9000" in cfg.mcp_server_urls


class TestMcpClientParseServers:
    def test_parse_servers_empty(self):
        from systemu.runtime.mcp.client import parse_servers
        assert parse_servers("") == []

    def test_parse_servers_single(self):
        from systemu.runtime.mcp.client import parse_servers
        assert parse_servers("http://localhost:8080") == ["http://localhost:8080"]

    def test_parse_servers_csv(self):
        from systemu.runtime.mcp.client import parse_servers
        result = parse_servers("http://a:1, http://b:2 ,http://c:3")
        assert result == ["http://a:1", "http://b:2", "http://c:3"]


class TestMcpCallTool:
    # v0.9.36 P2: mcp_call_tool now routes through the SDK-isolated
    # ConnectionManager (httpx dropped from the path). These cases monkeypatch
    # the manager's async call_tool so they exercise the new envelope contract
    # without a live server.
    @staticmethod
    def _patch_manager_call(monkeypatch, fake_async):
        from systemu.runtime.mcp.sdk import manager as _m
        monkeypatch.setattr(_m.ConnectionManager, "call_tool", fake_async)

    def test_mcp_call_tool_returns_response_on_success(self, monkeypatch):
        from systemu.runtime.mcp import client

        async def fake_call(self, server, spec, name, arguments=None):
            return {"success": True, "response": {"result": "ok", "value": 42}}
        self._patch_manager_call(monkeypatch, fake_call)

        from sharing_on.config import Config
        cfg = Config()
        result = client.mcp_call_tool(
            server="http://example.com",
            name="some_tool",
            params={"x": 1},
            config=cfg,
        )
        assert result["success"] is True
        assert result["response"]["result"] == "ok"

    def test_mcp_call_tool_handles_server_error(self, monkeypatch):
        from systemu.runtime.mcp import client

        async def fake_call(self, server, spec, name, arguments=None):
            return {"success": False, "error": "HTTP 500: internal error"}
        self._patch_manager_call(monkeypatch, fake_call)

        from sharing_on.config import Config
        cfg = Config()
        result = client.mcp_call_tool(
            server="http://example.com",
            name="some_tool", params={},
            config=cfg,
        )
        assert result["success"] is False
        assert "500" in result["error"]

    def test_mcp_call_tool_handles_network_exception(self, monkeypatch):
        from systemu.runtime.mcp import client

        async def fake_call(self, server, spec, name, arguments=None):
            return {"success": False, "error": "connection/call failed: refused"}
        self._patch_manager_call(monkeypatch, fake_call)

        from sharing_on.config import Config
        cfg = Config()
        result = client.mcp_call_tool(
            server="http://example.com",
            name="some_tool", params={},
            config=cfg,
        )
        assert result["success"] is False
        assert "connection" in result["error"].lower()


class TestMcpRegistered:
    def test_mcp_tool_registered(self):
        """The mcp_call_tool wrapper is registered in the v2 registry as
        a tool the LLM can invoke (with explicit server + name params)."""
        from systemu.runtime.tool_registry_v2 import registry as singleton
        import systemu.runtime.mcp.client  # noqa: F401
        entry = singleton.get("mcp_call_tool")
        assert entry is not None
        assert entry.toolset == "mcp"
