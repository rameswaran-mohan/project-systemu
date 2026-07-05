"""W9.3 — MCP connections: surfaced, gated, usable from the quick lane.

runtime/mcp/client.py shipped in v0.9.5 and stayed dormant: no UI surfaces
servers, no per-tool gating exists, and MCP tools (v2-registered) are
invisible to the quick lane. This slice adds a vault-persisted connections
store (servers + per-tool enable, OFF by default — Gate-3 parity), tolerant
tool discovery, and scoped quick-lane inclusion: ENABLED connector tools
join the index and dispatch through mcp_call_tool with the truth-in-results
envelope. Vault tools always win name collisions (a connector must never
shadow write_text_file).
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from systemu.vault.vault import Vault


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    for sub in ["tools/implementations", "elder"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    (tmp_path / "tools" / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))


class TestConnectionsStore:
    def test_servers_roundtrip(self, vault):
        from systemu.runtime.mcp import connections as cx
        assert cx.get_state(vault)["servers"] == []
        cx.add_server(vault, "http://localhost:9901")
        cx.add_server(vault, "http://localhost:9901")   # idempotent
        assert cx.get_state(vault)["servers"] == ["http://localhost:9901"]
        cx.remove_server(vault, "http://localhost:9901")
        assert cx.get_state(vault)["servers"] == []

    def test_tools_disabled_by_default_enable_persists_meta(self, vault):
        from systemu.runtime.mcp import connections as cx
        srv = "http://localhost:9901"
        assert cx.is_tool_enabled(vault, srv, "send_mail") is False
        cx.set_tool_enabled(vault, srv, "send_mail", True,
                            description="Send a mail", schema={"type": "object"})
        assert cx.is_tool_enabled(vault, srv, "send_mail") is True
        entries = cx.enabled_tools(vault)
        assert len(entries) == 1
        assert entries[0]["name"] == "send_mail"
        assert entries[0]["server"] == srv
        assert entries[0]["description"] == "Send a mail"
        # Disable removes it from the quick-lane surface.
        cx.set_tool_enabled(vault, srv, "send_mail", False)
        assert cx.enabled_tools(vault) == []

    def test_removing_server_disables_its_tools(self, vault):
        from systemu.runtime.mcp import connections as cx
        srv = "http://localhost:9901"
        cx.add_server(vault, srv)
        cx.set_tool_enabled(vault, srv, "t1", True, description="", schema={})
        cx.remove_server(vault, srv)
        assert cx.enabled_tools(vault) == []

    def test_env_servers_merge_readonly(self, vault):
        from systemu.runtime.mcp import connections as cx
        cx.add_server(vault, "http://localhost:9901")
        servers = cx.all_servers(
            vault, env={"SYSTEMU_MCP_SERVER_URLS": "http://envserver:1, http://localhost:9901"})
        assert servers == ["http://localhost:9901", "http://envserver:1"]

    def test_defensive_on_broken_vault(self):
        from systemu.runtime.mcp import connections as cx
        assert cx.get_state(object())["servers"] == []
        assert cx.enabled_tools(object()) == []


class _StubMCP(BaseHTTPRequestHandler):
    def _send(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        if self.path == "/tools/list":
            self._send({"tools": [
                {"name": "lookup_invoice", "description": "Find an invoice",
                 "inputSchema": {"type": "object",
                                 "properties": {"number": {"type": "string"}}}},
            ]})
        elif self.path == "/tools/call":
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
            self._send({"content": {"invoice": req.get("arguments", {}).get("number", ""),
                                    "status": "PAID"}})
        else:
            self._send({"error": "not found"}, code=404)

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def stub_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _StubMCP)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


class TestDiscovery:
    # v0.9.36 P2: discovery now speaks the MCP wire protocol via the SDK-isolated
    # ConnectionManager (the legacy REST /tools/list stub no longer applies).
    # These drive the manager directly; the wire-level round-trip is covered by
    # the hermetic reference server in tests/test_v0936_mcp_client.py.
    def test_list_tools_normalizes(self, monkeypatch):
        from systemu.runtime.mcp.sdk import manager as _m
        from systemu.runtime.mcp.client import mcp_list_tools

        async def fake_list(self, server, spec):
            return [{
                "name": "lookup_invoice",
                "description": "Find an invoice",
                "parameters_schema": {"type": "object",
                                      "properties": {"number": {"type": "string"}},
                                      "required": []},
                "annotations": {},
            }]
        monkeypatch.setattr(_m.ConnectionManager, "list_tools", fake_list)
        out = mcp_list_tools(server="http://stub", timeout=10)
        assert out["success"] is True
        assert len(out["tools"]) == 1
        tool = out["tools"][0]
        assert tool["name"] == "lookup_invoice"
        # description is now sanitised (untrusted-labelled) before it surfaces
        assert tool["description"].startswith("[untrusted MCP tool description]")
        assert "Find an invoice" in tool["description"]
        assert tool["schema"]["properties"]["number"] == {"type": "string"}

    def test_list_tools_unreachable_is_honest(self):
        from systemu.runtime.mcp.client import mcp_list_tools
        out = mcp_list_tools(server="http://127.0.0.1:1", timeout=2)
        # A real connection failure surfaces as an empty discovery (the manager
        # logs + returns []), so honesty here = no fabricated tools.
        assert out["success"] is True
        assert out["tools"] == []


class TestQuickLaneInclusion:
    def _llm(self, script):
        calls = {"payloads": [], "i": 0}

        def llm(*, system, user, config=None):
            calls["payloads"].append(user)
            action = script[min(calls["i"], len(script) - 1)]
            calls["i"] += 1
            return action
        llm.calls = calls
        return llm

    def test_enabled_mcp_tool_in_index_and_dispatches(self, vault, monkeypatch):
        from systemu.runtime.mcp import connections as cx
        from systemu.runtime.mcp.sdk import manager as _m
        from systemu.pipelines.quick_task import run_quick_task
        server = "http://stub"
        cx.add_server(vault, server)
        # v0.9.34: MCP action calls are gated; a read-only tool (readOnlyHint)
        # is Tier R → ungated, so this lookup dispatches directly as before.
        cx.set_tool_enabled(vault, server, "lookup_invoice", True,
                            description="Find an invoice",
                            schema={"type": "object"},
                            annotations={"readOnlyHint": True})
        # S1b Task 5: an UNPINNED (first-use) tool now gates regardless of
        # readOnlyHint. Pin it here to simulate a tool already seen before —
        # this test is about quick-lane dispatch/index inclusion, not the
        # first-use gate (see tests/test_s1b_mcp_firstuse.py for that).
        cx.set_tool_hash(vault, server, "lookup_invoice", "test-pinned-hash")

        # v0.9.36 P2: the call now routes through the SDK-isolated manager
        # (httpx REST stub dropped). Mock the manager's transport-level call_tool
        # to return the same PAID payload the legacy stub produced.
        async def fake_call(self, srv, spec, name, arguments=None):
            return {"success": True,
                    "response": {"invoice": (arguments or {}).get("number", ""),
                                 "status": "PAID"}}
        monkeypatch.setattr(_m.ConnectionManager, "call_tool", fake_call)

        llm = self._llm([
            {"action": "TOOL_CALL", "tool": "lookup_invoice",
             "params": {"number": "INV-42"}},
            {"action": "ANSWER", "answer_md": "found"},
        ])
        res = run_quick_task("find invoice 42", None, vault, llm_json=llm)
        assert res.status == "success" and res.tool_calls == 1
        # The index advertised the connector; the REAL connector response reached
        # the second turn (anti-no-op for connectors).
        assert "lookup_invoice" in llm.calls["payloads"][0]
        assert "PAID" in llm.calls["payloads"][1]

    def test_disabled_mcp_tool_invisible(self, vault, stub_server):
        from systemu.runtime.mcp import connections as cx
        from systemu.pipelines.quick_task import run_quick_task
        cx.add_server(vault, stub_server)   # server known, tool NOT enabled
        llm = self._llm([{"action": "ANSWER", "answer_md": "ok"}])
        run_quick_task("anything", None, vault, llm_json=llm)
        assert "lookup_invoice" not in llm.calls["payloads"][0]

    def test_vault_tool_wins_name_collision(self, vault, stub_server, tmp_path):
        """A connector must never shadow a local tool (hijack guard)."""
        from systemu.core.models import Tool, ToolStatus, ToolType
        from systemu.core.utils import generate_id
        from systemu.runtime.mcp import connections as cx
        from systemu.pipelines.quick_task import _enabled_tool_records, _tool_index
        impl = Path(vault.root) / "tools" / "implementations" / "lookup_invoice.py"
        impl.write_text("def run(**k):\n    return {'success': True}\n",
                        encoding="utf-8")
        vault.save_tool(Tool(id=generate_id("tool"), name="lookup_invoice",
                             description="local", tool_type=ToolType.PYTHON_FUNCTION,
                             status=ToolStatus.DEPLOYED, enabled=True,
                             implementation_path=str(impl)))
        cx.set_tool_enabled(vault, stub_server, "lookup_invoice", True,
                            description="remote", schema={})
        from systemu.pipelines.quick_task import _mcp_quick_entries
        entries = _mcp_quick_entries(vault, {t.name for t in _enabled_tool_records(vault)})
        assert entries == [], "colliding connector name must be dropped"

    def test_empty_connector_response_is_failure(self, vault, monkeypatch):
        """Truth-in-results applies to connectors too."""
        from systemu.runtime.mcp import connections as cx
        import systemu.runtime.mcp.client as client
        from systemu.pipelines.quick_task import run_quick_task
        # v0.9.34: read-only (Tier R) so the gate doesn't intercept; this test
        # is about empty-response-is-failure, not the approval gate.
        cx.set_tool_enabled(vault, "http://x", "ghost_tool", True,
                            description="", schema={},
                            annotations={"readOnlyHint": True})
        # S1b Task 5: pin the hash so this (otherwise first-use) tool is
        # classification_trusted and the Tier-R short-circuit still applies —
        # this test targets empty-response-is-failure, not first-use gating.
        cx.set_tool_hash(vault, "http://x", "ghost_tool", "test-pinned-hash")
        monkeypatch.setattr(client, "mcp_call_tool",
                            lambda **k: {"success": True, "response": {}})
        llm = self._llm([
            {"action": "TOOL_CALL", "tool": "ghost_tool", "params": {}},
            {"action": "ANSWER", "answer_md": "done"},
        ])
        res = run_quick_task("use ghost", None, vault, llm_json=llm)
        assert res.status == "success"
        # The transcript must record the call as a FAILURE, not phantom success.
        assert '"success": false' in llm.calls["payloads"][1].lower()


class TestSettingsWiring:
    def test_connections_section_present(self):
        import inspect
        from systemu.interface.pages import settings
        src = inspect.getsource(settings)
        assert "MCP" in src and "add_server" in src and "set_tool_enabled" in src