"""D-MCP2 -- systemu's MCP client layer runs on BOTH official-SDK major lines.

mcp 2.x removed ``mcp.client.streamable_http.streamablehttp_client`` and
``mcp.server.fastmcp``, split the lowlevel ``Server`` registration from
decorators to ``on_*`` constructor handlers, and moved every SDK model to
snake_case attributes behind camelCase wire aliases. 0.10.32 pinned ``mcp<2``.

Each section below pins one part of the port, on whichever line is installed
(the witness runs the file once in a clean venv per line):

* the ONE seam (``systemu.runtime.mcp.sdk.compat``) picks the transport factory
  / server class / lowlevel style by FEATURE DETECTION, 2.x names first, then
  1.x names. Both module layouts are faked by ``sys.modules`` injection AT THE
  PROBE (never by stubbing the product) and the real probe runs against them;
* operator headers reach the wire, and an unreachable server is the honest
  connection-failure envelope, never a missing-SDK-symbol error;
* SDK models are read by WIRE name: on 2.x ``getattr(result, "isError", False)``
  is always False, so a failed tool call reported success;
* real servers (stdio / streamable HTTP / SSE), the tool-error envelope,
  annotations, and server-initiated elicitation through the ConnectionManager;
* the OAuth fallback provider and its token storage.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
import types
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REF_SERVER = str(Path(__file__).resolve().parent / "_mcp_reference_server.py")


# ---------------------------------------------------------------------------
# Probe seam: both module layouts faked by sys.modules injection.
# ---------------------------------------------------------------------------

_PROBED = (
    "mcp.client.streamable_http",
    "mcp.shared._httpx_utils",
    "mcp.client.stdio",
    "mcp.client.sse",
    "mcp.server.mcpserver",
    "mcp.server.fastmcp",
    "mcp.server.lowlevel",
)


def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def _stdio_client(params):  # pragma: no cover - identity only
    raise AssertionError("not called")


def _sse_client(url, headers=None):  # pragma: no cover - identity only
    raise AssertionError("not called")


class _HandlerStyleServer:
    """mcp 2.x lowlevel Server: handlers are constructor kwargs."""

    def __init__(self, name, *, on_list_tools=None, on_call_tool=None):
        self.name = name


class _DecoratorStyleServer:
    """mcp 1.x lowlevel Server: handlers are registered by decorators."""

    def __init__(self, name):
        self.name = name

    def list_tools(self):  # pragma: no cover - identity only
        return lambda f: f

    def call_tool(self):  # pragma: no cover - identity only
        return lambda f: f


def _layout_2x():
    """mcp 2.x: modern factory + SDK http-client builder, MCPServer, on_* Server.
    The 1.x names are ABSENT (fastmcp import fails, legacy factory missing)."""
    def streamable_http_client(url, *, http_client=None, terminate_on_close=True):
        raise AssertionError("identity only")

    def create_mcp_http_client(headers=None, timeout=None, auth=None):
        raise AssertionError("identity only")

    class MCPServer:
        pass

    class Context:
        pass

    return {
        "mcp.client.streamable_http": _mod(
            "mcp.client.streamable_http", streamable_http_client=streamable_http_client),
        "mcp.shared._httpx_utils": _mod(
            "mcp.shared._httpx_utils", create_mcp_http_client=create_mcp_http_client),
        "mcp.client.stdio": _mod("mcp.client.stdio", stdio_client=_stdio_client),
        "mcp.client.sse": _mod("mcp.client.sse", sse_client=_sse_client),
        "mcp.server.mcpserver": _mod("mcp.server.mcpserver", MCPServer=MCPServer, Context=Context),
        "mcp.server.fastmcp": None,  # import raises ModuleNotFoundError
        "mcp.server.lowlevel": _mod("mcp.server.lowlevel", Server=_HandlerStyleServer),
    }


def _layout_1x_legacy():
    """mcp <=1.23: ONLY the legacy factory; FastMCP; decorator-style Server."""
    def streamablehttp_client(url, headers=None, timeout=30, sse_read_timeout=300,
                              terminate_on_close=True, httpx_client_factory=None, auth=None):
        raise AssertionError("identity only")

    class FastMCP:
        pass

    class Context:
        pass

    return {
        "mcp.client.streamable_http": _mod(
            "mcp.client.streamable_http", streamablehttp_client=streamablehttp_client),
        "mcp.shared._httpx_utils": _mod("mcp.shared._httpx_utils"),
        "mcp.client.stdio": _mod("mcp.client.stdio", stdio_client=_stdio_client),
        "mcp.client.sse": _mod("mcp.client.sse", sse_client=_sse_client),
        "mcp.server.mcpserver": None,
        "mcp.server.fastmcp": _mod("mcp.server.fastmcp", FastMCP=FastMCP, Context=Context),
        "mcp.server.lowlevel": _mod("mcp.server.lowlevel", Server=_DecoratorStyleServer),
    }


def _inject(monkeypatch, layout):
    for name in _PROBED:
        monkeypatch.setitem(sys.modules, name, layout.get(name))
    # A version string that LIES about the line: the probe must not read it.
    monkeypatch.setitem(sys.modules, "mcp", _mod("mcp", __version__="0.0.0-not-consulted"))


class TestProbeSeam:
    def test_2x_layout_selects_the_2x_streamable_http_factory(self, monkeypatch):
        from systemu.runtime.mcp.sdk import compat
        layout = _layout_2x()
        _inject(monkeypatch, layout)
        got = compat._probe()
        mod = layout["mcp.client.streamable_http"]
        assert got.streamable_http_api == "streamable_http_client"
        assert got.streamable_http_factory is mod.streamable_http_client
        assert got.http_client_factory is layout["mcp.shared._httpx_utils"].create_mcp_http_client

    def test_1x_layout_selects_the_1x_streamable_http_factory(self, monkeypatch):
        from systemu.runtime.mcp.sdk import compat
        layout = _layout_1x_legacy()
        _inject(monkeypatch, layout)
        got = compat._probe()
        mod = layout["mcp.client.streamable_http"]
        assert got.streamable_http_api == "streamablehttp_client"
        assert got.streamable_http_factory is mod.streamablehttp_client
        assert got.http_client_factory is None

    def test_2x_names_win_when_both_lines_names_are_present(self, monkeypatch):
        # mcp 1.24..1.30 ship BOTH names (the old one deprecated). 2.x names first.
        from systemu.runtime.mcp.sdk import compat
        layout = _layout_2x()
        legacy = _layout_1x_legacy()["mcp.client.streamable_http"].streamablehttp_client
        layout["mcp.client.streamable_http"].streamablehttp_client = legacy
        _inject(monkeypatch, layout)
        got = compat._probe()
        assert got.streamable_http_api == "streamable_http_client"
        assert got.streamable_http_factory is \
            layout["mcp.client.streamable_http"].streamable_http_client

    def test_modern_factory_without_the_sdk_client_builder_falls_back(self, monkeypatch):
        # The 2.x factory takes an http_client; without the SDK's builder systemu
        # cannot thread headers into it, so it must not be selected half-wired.
        from systemu.runtime.mcp.sdk import compat
        layout = _layout_2x()
        legacy = _layout_1x_legacy()["mcp.client.streamable_http"].streamablehttp_client
        layout["mcp.client.streamable_http"].streamablehttp_client = legacy
        layout["mcp.shared._httpx_utils"] = _mod("mcp.shared._httpx_utils")
        _inject(monkeypatch, layout)
        got = compat._probe()
        assert got.streamable_http_api == "streamablehttp_client"
        assert got.streamable_http_factory is legacy

    def test_server_class_and_lowlevel_style_follow_the_layout(self, monkeypatch):
        from systemu.runtime.mcp.sdk import compat
        l2 = _layout_2x()
        _inject(monkeypatch, l2)
        got2 = compat._probe()
        assert got2.server_api == "MCPServer"
        assert got2.server_class is l2["mcp.server.mcpserver"].MCPServer
        assert got2.context_class is l2["mcp.server.mcpserver"].Context
        assert got2.lowlevel_api == "on_handlers"

        l1 = _layout_1x_legacy()
        _inject(monkeypatch, l1)
        got1 = compat._probe()
        assert got1.server_api == "FastMCP"
        assert got1.server_class is l1["mcp.server.fastmcp"].FastMCP
        assert got1.context_class is l1["mcp.server.fastmcp"].Context
        assert got1.lowlevel_api == "decorators"

    def test_stdio_and_sse_constructors_come_from_the_seam(self, monkeypatch):
        from systemu.runtime.mcp.sdk import compat
        _inject(monkeypatch, _layout_2x())
        got = compat._probe()
        assert got.stdio_factory is _stdio_client
        assert got.sse_factory is _sse_client

    def test_no_streamable_http_name_is_a_named_error_not_a_crash(self, monkeypatch):
        from systemu.runtime.mcp.sdk import compat
        layout = _layout_2x()
        layout["mcp.client.streamable_http"] = _mod("mcp.client.streamable_http")
        _inject(monkeypatch, layout)
        got = compat._probe()  # probing itself never raises
        assert got.streamable_http_factory is None
        with pytest.raises(compat.SdkLayoutError) as ei:
            got.require("streamable_http_factory")
        # the error names BOTH probed names so an operator can act on it
        assert "streamable_http_client" in str(ei.value)
        assert "streamablehttp_client" in str(ei.value)

    def test_probe_runs_at_seam_import_and_matches_the_installed_sdk(self):
        # LAYOUT is computed when the seam module is imported; on the installed
        # SDK the probe must have found a usable streamable-HTTP factory.
        import importlib
        from systemu.runtime.mcp.sdk import compat
        assert compat.LAYOUT.streamable_http_factory is not None
        installed = importlib.import_module("mcp.client.streamable_http")
        assert getattr(installed, compat.LAYOUT.streamable_http_api) is \
            compat.LAYOUT.streamable_http_factory
        try:
            importlib.import_module("mcp.server.mcpserver")
            has_2x_server = True
        except ImportError:
            has_2x_server = False
        assert compat.LAYOUT.server_api == ("MCPServer" if has_2x_server else "FastMCP")


@pytest.mark.asyncio
class TestStreamableHttpFactoryCalls:
    """The selected factory is CALLED the way its line expects."""

    async def test_2x_factory_gets_an_sdk_built_client_carrying_the_headers(self, monkeypatch):
        from contextlib import asynccontextmanager
        from systemu.runtime.mcp.sdk import compat
        calls = []

        class _Client:
            def __init__(self, **kw):
                calls.append(("client", kw))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                calls.append(("client_closed", None))
                return False

        def create_mcp_http_client(headers=None, timeout=None, auth=None):
            return _Client(headers=headers, auth=auth)

        @asynccontextmanager
        async def streamable_http_client(url, *, http_client=None, terminate_on_close=True):
            calls.append(("factory", url, http_client))
            yield ("R", "W")

        layout = _layout_2x()
        layout["mcp.client.streamable_http"].streamable_http_client = streamable_http_client
        layout["mcp.shared._httpx_utils"].create_mcp_http_client = create_mcp_http_client
        _inject(monkeypatch, layout)
        got = compat._probe()
        monkeypatch.undo()  # the open path must use the layout, not live imports

        auth = object()
        async with compat.open_streamable_http(
                "https://h.example/mcp", headers={"Authorization": "Bearer t"},
                auth=auth, layout=got) as streams:
            assert streams == ("R", "W")
        assert [c[0] for c in calls] == ["client", "factory", "client_closed"]
        assert calls[0][1] == {"headers": {"Authorization": "Bearer t"}, "auth": auth}
        assert calls[1][1] == "https://h.example/mcp"
        assert isinstance(calls[1][2], _Client)

    async def test_1x_factory_gets_url_and_headers_directly(self, monkeypatch):
        from contextlib import asynccontextmanager
        from systemu.runtime.mcp.sdk import compat
        calls = []

        @asynccontextmanager
        async def streamablehttp_client(url, headers=None, timeout=30, sse_read_timeout=300,
                                        terminate_on_close=True, httpx_client_factory=None,
                                        auth=None):
            calls.append({"url": url, "headers": headers, "auth": auth})
            yield ("R", "W", lambda: None)

        layout = _layout_1x_legacy()
        layout["mcp.client.streamable_http"].streamablehttp_client = streamablehttp_client
        _inject(monkeypatch, layout)
        got = compat._probe()
        monkeypatch.undo()

        async with compat.open_streamable_http(
                "https://h.example/mcp", headers={"X-K": "v"}, layout=got) as streams:
            assert streams[:2] == ("R", "W")
        assert calls == [{"url": "https://h.example/mcp", "headers": {"X-K": "v"}, "auth": None}]


# ---------------------------------------------------------------------------
# The seam on a real socket: operator headers and the honest-failure envelope.
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextmanager
def _header_recorder():
    """A plain HTTP endpoint that records the request headers it receives and
    answers 503, so the MCP connect fails AFTER the headers hit the wire."""
    seen = []

    class _H(BaseHTTPRequestHandler):
        def _record(self):
            seen.append({k.lower(): v for k, v in self.headers.items()})
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST = do_DELETE = _record

        def log_message(self, *a):  # silence
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.asyncio
class TestTransportSeamOnTheWire:
    @pytest.mark.parametrize("spec_transport,path", [("http", "/mcp"), ("sse", "/sse")])
    async def test_operator_headers_reach_the_wire(self, spec_transport, path):
        from systemu.runtime.mcp.sdk.manager import ConnectionManager
        mgr = ConnectionManager(default_timeout=15.0)
        with _header_recorder() as (base, seen):
            spec = {"transport": spec_transport, "url": base + path,
                    "headers": {"X-Systemu-Probe": "probe-value-7"}}
            out = await mgr.call_tool("hdr", spec, "echo", {})
        assert out["success"] is False  # the endpoint is not an MCP server
        assert seen, "no request reached the endpoint"
        assert seen[0].get("x-systemu-probe") == "probe-value-7", seen[0]

    async def test_unreachable_server_is_the_honest_envelope_not_a_missing_symbol(self):
        from systemu.runtime.mcp import client as mc
        t0 = time.monotonic()
        out = mc.mcp_call_tool(server=f"http://127.0.0.1:{_free_port()}/mcp",
                               name="x", params={}, config=None)
        assert set(out) == {"success", "error"}
        assert out["success"] is False
        assert out["error"].startswith("connection/call failed:")
        # the failure is the CONNECTION, never the SDK layout
        for marker in ("cannot import", "has no attribute", "No module named",
                       "SdkLayoutError"):
            assert marker not in out["error"], out["error"]
        assert time.monotonic() - t0 < 25


# ---------------------------------------------------------------------------
# Wire fields: real SDK objects of the installed line.
# ---------------------------------------------------------------------------

class TestWireFields:
    def test_tool_error_result_is_an_error_envelope(self):
        import mcp.types as t
        from systemu.runtime.mcp.sdk.manager import _result_to_envelope
        res = t.CallToolResult(content=[t.TextContent(type="text", text="it broke")],
                               isError=True)
        assert _result_to_envelope(res) == {"success": False, "error": "it broke"}

    def test_structured_content_is_the_response_body(self):
        import mcp.types as t
        from systemu.runtime.mcp.sdk.manager import _result_to_envelope
        res = t.CallToolResult(content=[t.TextContent(type="text", text="{}")],
                               structuredContent={"html_url": "https://1.2.3.4/i/1"})
        assert _result_to_envelope(res) == {
            "success": True, "response": {"html_url": "https://1.2.3.4/i/1"}}

    def test_tool_annotations_survive_normalisation(self):
        import mcp.types as t
        from systemu.runtime.mcp.sdk.manager import _normalise_tool
        tool = t.Tool(name="rm", description="remove",
                      inputSchema={"type": "object", "properties": {"p": {"type": "string"}},
                                   "required": ["p"]},
                      annotations=t.ToolAnnotations(destructiveHint=True, readOnlyHint=False))
        out = _normalise_tool(tool)
        assert out["annotations"] == {"destructiveHint": True, "readOnlyHint": False}
        assert out["parameters_schema"]["required"] == ["p"]

    def test_to_systemu_schema_reads_annotations_off_a_real_tool(self):
        import mcp.types as t
        from systemu.runtime.mcp.sdk.schema_map import to_systemu_schema
        tool = t.Tool(name="ls", description="list", inputSchema={"type": "object"},
                      annotations=t.ToolAnnotations(readOnlyHint=True))
        assert to_systemu_schema(tool)["annotations"] == {"readOnlyHint": True}

    def test_wire_field_reads_both_attribute_spellings_and_dicts(self):
        from systemu.runtime.mcp.sdk.schema_map import wire_field

        class OneX:
            isError = True

        class TwoX:
            is_error = True

        assert wire_field(OneX(), "isError", False) is True
        assert wire_field(TwoX(), "isError", False) is True
        assert wire_field({"isError": True}, "isError", False) is True
        assert wire_field({"is_error": True}, "isError", False) is True
        assert wire_field(object(), "isError", "dflt") == "dflt"
        assert wire_field(None, "isError", "dflt") == "dflt"


# ---------------------------------------------------------------------------
# Real servers on the installed line (dual-line reference server).
# ---------------------------------------------------------------------------

@contextmanager
def _reference_server(transport: str):
    """Spawn the dual-line reference server over a REAL socket; yield its URL."""
    import tempfile
    port = _free_port()
    errlog = tempfile.TemporaryFile()  # a file, not a pipe: server logs never block it
    proc = subprocess.Popen([sys.executable, REF_SERVER, transport, str(port)],
                            stdout=subprocess.DEVNULL, stderr=errlog)
    try:
        deadline = time.monotonic() + 60
        while True:
            if proc.poll() is not None:
                errlog.seek(0)
                err = errlog.read().decode("utf-8", "backslashreplace")
                raise AssertionError(f"reference server exited early: {err[-2000:]}")
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise AssertionError("reference server never listened")
                time.sleep(0.1)
        path = "/mcp" if transport == "streamable-http" else "/sse"
        yield f"http://127.0.0.1:{port}{path}"
    finally:
        proc.kill()
        proc.wait(timeout=30)
        errlog.close()


def _stdio_spec():
    return {"transport": "stdio", "command": sys.executable, "args": [REF_SERVER], "env": {}}


@pytest.mark.asyncio
class TestRealTransports:
    @pytest.mark.parametrize("transport,spec_transport", [
        ("streamable-http", "http"),
        ("sse", "sse"),
    ])
    async def test_remote_round_trip_through_the_manager(self, transport, spec_transport):
        from systemu.runtime.mcp.sdk.manager import ConnectionManager
        mgr = ConnectionManager(default_timeout=30.0)
        with _reference_server(transport) as url:
            spec = {"transport": spec_transport, "url": url}
            out = await mgr.connect_and_discover("ref-remote", spec,
                                                 allowed_hosts={"127.0.0.1"})
            assert out["error"] is None, out
            assert out["connected"] is True
            names = {t["name"] for t in out["tools"]}
            assert {"echo", "delete_thing", "create_note"} <= names
            call = await mgr.call_tool("ref-remote", spec, "echo", {"text": "over-the-wire"})
            assert call["success"] is True, call
            assert "over-the-wire" in json.dumps(call["response"])

    async def test_tool_error_is_an_error_envelope_over_stdio(self):
        from systemu.runtime.mcp.sdk.manager import ConnectionManager
        mgr = ConnectionManager()
        out = await mgr.call_tool("ref", _stdio_spec(), "fail_always", {})
        assert out["success"] is False, out
        assert "fail_always" in out["error"]

    async def test_destructive_and_readonly_hints_survive_discovery(self):
        from systemu.runtime.mcp.sdk.manager import ConnectionManager
        mgr = ConnectionManager()
        tools = {t["name"]: t for t in await mgr.list_tools("ref", _stdio_spec())}
        assert tools["delete_thing"]["annotations"].get("destructiveHint") is True
        assert tools["echo"]["annotations"].get("readOnlyHint") is True
        assert tools["create_note"]["annotations"] == {}
        assert tools["create_note"]["parameters_schema"]["required"] == ["title"]

    async def test_server_elicitation_accept_reaches_the_server(self, monkeypatch):
        import systemu.runtime.elicitation as elic
        from systemu.runtime.mcp.sdk.manager import ConnectionManager
        captured = {}

        def resolver(*, message, requested_schema):
            captured["message"] = message
            captured["schema"] = requested_schema
            return {"action": "accept", "content": {"name": "Ada"}}

        monkeypatch.setattr(elic, "resolve_structured_input", resolver, raising=False)
        mgr = ConnectionManager()
        mgr.build_elicitation_callback()
        out = await mgr.call_tool("ref", _stdio_spec(), "ask_name", {})
        assert out["success"] is True, out
        assert "accept:Ada" in json.dumps(out["response"])
        assert captured["message"].startswith("[untrusted MCP tool description]")
        assert "What is your name?" in captured["message"]
        assert captured["schema"]["properties"]["name"]["type"] == "string"

    async def test_server_elicitation_declines_fail_closed(self, monkeypatch):
        import systemu.runtime.elicitation as elic
        from systemu.runtime.mcp.sdk.manager import ConnectionManager

        def boom(*, message, requested_schema):
            raise RuntimeError("headless")

        monkeypatch.setattr(elic, "resolve_structured_input", boom, raising=False)
        mgr = ConnectionManager()
        mgr.build_elicitation_callback()
        out = await mgr.call_tool("ref", _stdio_spec(), "ask_name", {})
        assert out["success"] is True, out
        assert "decline:" in json.dumps(out["response"])
        assert "Ada" not in json.dumps(out["response"])


# ---------------------------------------------------------------------------
# OAuth fallback: the SDK provider is constructible on the installed line and
# its token storage speaks the SDK's TokenStorage protocol (same on both lines).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestOAuthProvider:
    async def test_builds_the_sdk_provider_on_the_installed_line(self, tmp_path):
        from mcp.client.auth import OAuthClientProvider
        from systemu.runtime.mcp.sdk import oauth
        vault = types.SimpleNamespace(root=str(tmp_path))
        prov = oauth.build_sdk_oauth_provider(
            "srv-1", vault=vault, authorization_url="https://mcp.example/mcp")
        assert isinstance(prov, OAuthClientProvider)
        assert isinstance(prov.context.storage, oauth.VaultOAuthStorage)

    async def test_storage_round_trips_sdk_models_through_the_vault_store(self, tmp_path):
        from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
        from systemu.runtime.mcp.sdk import oauth
        vault = types.SimpleNamespace(root=str(tmp_path))
        storage = oauth.VaultOAuthStorage(oauth.VaultTokenStore(vault, "srv-1"))
        assert await storage.get_tokens() is None
        assert await storage.get_client_info() is None

        await storage.set_tokens(OAuthToken(access_token="access-zz9", token_type="Bearer",
                                            refresh_token="refresh-zz9", expires_in=3600))
        await storage.set_client_info(OAuthClientInformationFull(
            client_id="client-zz9", redirect_uris=["http://127.0.0.1/callback"]))

        # a FRESH adapter over the same vault reads both back as SDK models
        again = oauth.VaultOAuthStorage(oauth.VaultTokenStore(vault, "srv-1"))
        tok = await again.get_tokens()
        info = await again.get_client_info()
        assert isinstance(tok, OAuthToken)
        assert (tok.access_token, tok.refresh_token) == ("access-zz9", "refresh-zz9")
        assert isinstance(info, OAuthClientInformationFull)
        assert info.client_id == "client-zz9"
        path = oauth.VaultTokenStore(vault, "srv-1").path
        assert path.parent.name == "mcp_oauth"


# ---------------------------------------------------------------------------
# The dependency pin admits exactly the verified range.
# ---------------------------------------------------------------------------

def test_mcp_pin_admits_both_verified_lines_and_nothing_unverified():
    """D-MCP2: the pin was widened from ``mcp<2`` only after the full MCP test
    set passed in clean venvs on the latest 2.x (2.3.0), the latest 1.x (1.30.0)
    and the floor 1.23.0 (the newest 1.x carrying ONLY the legacy
    streamable-HTTP name, i.e. the probe's 1.x branch). Nothing below the floor
    and no 3.x was verified, so neither may resolve."""
    try:
        import tomllib
    except ImportError:  # Python 3.10: the [dev] extra ships tomli
        import tomli as tomllib

    from packaging.requirements import Requirement
    root = Path(__file__).resolve().parent.parent
    deps = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"]["dependencies"]
    reqs = [Requirement(d) for d in deps if Requirement(d).name == "mcp"]
    assert len(reqs) == 1, reqs
    spec = reqs[0].specifier
    for verified in ("1.23.0", "1.30.0", "2.3.0"):
        assert spec.contains(verified), (verified, str(spec))
    for unverified in ("1.22.0", "1.0.0", "3.0.0"):
        assert not spec.contains(unverified), (unverified, str(spec))
