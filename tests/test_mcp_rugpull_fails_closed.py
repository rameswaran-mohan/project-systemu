"""R-3 -- the pre-call rug-pull check fails CLOSED.

Before every MCP call, ``dispatch.call_mcp_tool`` re-lists the server and
compares the tool's current definition with the hash pinned when the operator
approved it. Witnessed defect: when that listing FAILED (server down, refused by
the SSRF guard, crashed, or the lister raised), the check was skipped and the
call went ahead -- the drift check silently became a pass. DEC-32: a failed
check is not a pass. A call whose pre-call listing fails is now REFUSED with the
listing's reason, and nothing is sent.

Hermetic: a vault under ``tmp_path``; a loopback HTTP endpoint (explicitly
allowed) that records every JSON-RPC method it receives and is not an MCP
server, so its listing fails for a real reason.
"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class _Vault:
    def __init__(self, root):
        self.root = str(root)


class _Cfg:
    check_fn_cache_ttl_seconds = 30


@contextmanager
def _recording_endpoint():
    """Answers 503 to everything; records the JSON-RPC method of each POST."""
    methods = []

    class _H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            try:
                methods.append(json.loads(body.decode("utf-8")).get("method"))
            except Exception:
                methods.append(None)
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            methods.append("GET")
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_DELETE = do_GET

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield srv.server_address[1], methods
    finally:
        srv.shutdown()
        srv.server_close()


def _enable_trusted_readonly(vault, server, tool):
    """Enabled, read-only and PINNED: the L3 gate lets it through, so the only
    thing that can stop the call is the rug-pull check under test."""
    from systemu.runtime.mcp import connections as c
    c.set_tool_enabled(vault, server, tool, True, description="d", schema={},
                       annotations={"readOnlyHint": True})
    c.set_tool_hash(vault, server, tool, "pinned-at-approval")


@pytest.fixture(autouse=True)
def _no_env_servers(monkeypatch):
    monkeypatch.setenv("SYSTEMU_MCP_SERVER_URLS", "")


class TestTheCheckFailsClosed:
    def test_a_failed_listing_refuses_the_call_and_sends_nothing(self, tmp_path, monkeypatch):
        from systemu.runtime.mcp import dispatch
        import systemu.runtime.mcp.client as mc
        sent = []
        real_call = mc.mcp_call_tool

        def _spy_call(**kw):
            sent.append(kw)
            return real_call(**kw)

        monkeypatch.setattr(mc, "mcp_call_tool", _spy_call)
        with _recording_endpoint() as (port, methods):
            server = f"http://127.0.0.1:{port}"
            monkeypatch.setenv("SYSTEMU_HARNESS_ALLOWED_MCP_HOSTS", f"127.0.0.1:{port}")
            v = _Vault(tmp_path)
            _enable_trusted_readonly(v, server, "read_inbox")
            out = dispatch.call_mcp_tool(server, "read_inbox", {}, vault=v,
                                         config=_Cfg(), session_id="run_A")
        assert out["success"] is False, out
        assert out.get("error_type") == "mcp_rug_pull_check_failed", out
        err = out["error"]
        assert err.startswith(f"Could not check that read_inbox is unchanged on {server}:"), err
        # the LISTING's own reason -- mcp 1.x keeps the HTTP status, mcp 2.x
        # reports the SDK's own "error response" (it drops the status)
        assert ("answered HTTP 503" in err
                or "MCPError: Server returned an error response" in err), err
        assert "no running event loop" not in err, err
        assert err.endswith("Nothing was sent."), err
        assert sent == [], "the call must never be handed to the transport"
        assert "tools/call" not in methods, methods     # 0 call hits on the server
        assert "initialize" in methods, "non-vacuous: the listing really ran"

    def test_a_listing_refused_by_the_ssrf_guard_refuses_the_call(self, tmp_path, monkeypatch):
        from systemu.runtime.mcp import dispatch
        import systemu.runtime.mcp.client as mc
        sent = []
        monkeypatch.setattr(mc, "mcp_call_tool", lambda **kw: sent.append(kw) or
                            {"success": True, "response": "should never run"})
        monkeypatch.delenv("SYSTEMU_HARNESS_ALLOWED_MCP_HOSTS", raising=False)
        v = _Vault(tmp_path)
        server = "http://169.254.169.254"
        _enable_trusted_readonly(v, server, "read_inbox")
        out = dispatch.call_mcp_tool(server, "read_inbox", {}, vault=v,
                                     config=_Cfg(), session_id="run_A")
        assert out["success"] is False
        assert "SSRF" in out["error"] and out["error"].endswith("Nothing was sent.")
        assert sent == []

    def test_a_listing_that_raises_refuses_the_call(self, tmp_path, monkeypatch):
        from systemu.runtime.mcp import dispatch
        import systemu.runtime.mcp.client as mc
        sent = []

        def _boom(**kw):
            raise RuntimeError("lister exploded")

        monkeypatch.setattr(mc, "mcp_list_tools", _boom)
        monkeypatch.setattr(mc, "mcp_call_tool", lambda **kw: sent.append(kw) or
                            {"success": True, "response": "should never run"})
        v = _Vault(tmp_path)
        _enable_trusted_readonly(v, "http://h.example.test", "read_inbox")
        out = dispatch.call_mcp_tool("http://h.example.test", "read_inbox", {}, vault=v,
                                     config=_Cfg(), session_id="run_A")
        assert out["success"] is False
        assert "lister exploded" in out["error"] and out["error"].endswith("Nothing was sent.")
        assert out["error"].isascii()
        assert sent == []

    def test_a_listing_without_the_tool_refuses_the_call(self, tmp_path, monkeypatch):
        """Ruled residual: a successful listing that no longer offers the tool is
        not a pass either -- a server can hide a tool from the listing and still
        serve it, so the call is refused and nothing is sent."""
        from systemu.runtime.mcp import dispatch
        import systemu.runtime.mcp.client as mc
        sent = []
        monkeypatch.setattr(mc, "mcp_list_tools", lambda **kw: {
            "success": True, "tools": [{"name": "something_else", "description": "d",
                                        "schema": {}}]})
        monkeypatch.setattr(mc, "mcp_call_tool", lambda **kw: sent.append(kw) or
                            {"success": True, "response": "should never run"})
        v = _Vault(tmp_path)
        _enable_trusted_readonly(v, "http://h.example.test", "read_inbox")
        out = dispatch.call_mcp_tool("http://h.example.test", "read_inbox", {}, vault=v,
                                     config=_Cfg(), session_id="run_A")
        assert out["success"] is False, out
        assert out.get("error_type") == "mcp_rug_pull_check_failed", out
        assert out["error"] == ("read_inbox is no longer offered by http://h.example.test. "
                                "Nothing was sent."), out
        assert sent == []

    def test_an_unchanged_listing_lets_the_call_through(self, tmp_path, monkeypatch):
        """Control: the check passes only on a real, matching listing."""
        from systemu.runtime.mcp import connections as c
        from systemu.runtime.mcp import dispatch
        from systemu.runtime.mcp.sdk.schema_map import tool_def_hash
        import systemu.runtime.mcp.client as mc
        sent = []
        monkeypatch.setattr(mc, "mcp_list_tools", lambda **kw: {
            "success": True, "tools": [{"name": "read_inbox", "description": "d",
                                        "schema": {}}]})
        monkeypatch.setattr(mc, "mcp_call_tool", lambda **kw: sent.append(kw) or
                            {"success": True, "response": "inbox: 0"})
        v = _Vault(tmp_path)
        _enable_trusted_readonly(v, "http://h.example.test", "read_inbox")
        c.set_tool_hash(v, "http://h.example.test", "read_inbox",
                        tool_def_hash(name="read_inbox", description="d", input_schema={}))
        out = dispatch.call_mcp_tool("http://h.example.test", "read_inbox", {}, vault=v,
                                     config=_Cfg(), session_id="run_A")
        assert out["success"] is True, out
        assert len(sent) == 1
