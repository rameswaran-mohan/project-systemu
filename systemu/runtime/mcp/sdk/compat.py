"""D-MCP2 -- the ONE seam that decides which official-`mcp` SDK line is installed.

systemu supports BOTH major lines of the SDK during the 1.x -> 2.x transition.
Every name that differs between the lines is resolved HERE, once, by FEATURE
DETECTION -- the mcp 2.x name is probed first, then the mcp 1.x name. Version
strings are never consulted: a backport, a pre-release or a vendored copy would
make them lie, while the presence of a callable cannot.

The probe runs when this module is imported (``LAYOUT``). Like everything in
``runtime/mcp/sdk``, the module is itself only ever imported lazily, from inside
a function, so importing systemu never imports the SDK and never connects.

What differs, and what each line exposes (full MCP test set verified on mcp
2.3.0, 1.30.0 and 1.23.0 -- the last exercising the 1.x-only branch):

  streamable HTTP client
    2.x name: ``mcp.client.streamable_http.streamable_http_client(url, *,
              http_client=...)`` -- headers/auth live on an HTTP client built
              by the SDK's own ``mcp.shared._httpx_utils.create_mcp_http_client``
              (the SDK owns which HTTP library that is: httpx on 1.x, httpx2 on
              2.x). mcp 1.24-1.30 ship it too, beside the deprecated old name.
    1.x name: ``streamablehttp_client(url=, headers=, auth=)`` -- REMOVED in 2.x;
              the only name on mcp <= 1.23.
  stdio / SSE client
    ``mcp.client.stdio.stdio_client`` / ``mcp.client.sse.sse_client`` -- same
    names and call shape on both lines; resolved here anyway so every transport
    constructor has one owner.
  high-level server (hermetic test reference servers only)
    2.x: ``mcp.server.mcpserver.MCPServer`` + ``Context``.
    1.x: ``mcp.server.fastmcp.FastMCP`` + ``Context`` (on 2.x the
         ``mcp.server.fastmcp`` module exists only to raise ModuleNotFoundError).
  lowlevel ``mcp.server.lowlevel.Server`` registration
    2.x: ``on_list_tools=`` / ``on_call_tool=`` constructor handlers
         ``(ctx, params)``.  1.x: ``@server.list_tools()`` / ``@server.call_tool()``
         decorators.
  SDK model attributes
    1.x models expose the camelCase wire name (``isError``); 2.x models expose
    snake_case (``is_error``) behind a camelCase alias, so a getattr on the wire
    name silently returns the default. Readers use
    ``schema_map.wire_field`` (MCP-free) for that.

Probing never raises: a missing name leaves its slot ``None`` and the first USE
raises :class:`SdkLayoutError` naming every name that was tried, which the
callers turn into their honest-failure envelope.
"""
from __future__ import annotations

import importlib
import inspect
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

# (module, attribute) pairs, in probe order: the 2.x name FIRST.
_STREAMABLE_HTTP_2X = ("mcp.client.streamable_http", "streamable_http_client")
_HTTP_CLIENT_2X = ("mcp.shared._httpx_utils", "create_mcp_http_client")
_STREAMABLE_HTTP_1X = ("mcp.client.streamable_http", "streamablehttp_client")
_STDIO = ("mcp.client.stdio", "stdio_client")
_SSE = ("mcp.client.sse", "sse_client")
_SERVER_2X = ("mcp.server.mcpserver", "MCPServer")
_CONTEXT_2X = ("mcp.server.mcpserver", "Context")
_SERVER_1X = ("mcp.server.fastmcp", "FastMCP")
_CONTEXT_1X = ("mcp.server.fastmcp", "Context")
_LOWLEVEL = ("mcp.server.lowlevel", "Server")

_TRIED: Dict[str, Tuple[str, ...]] = {
    "streamable_http_factory": (
        ".".join(_STREAMABLE_HTTP_2X) + " (+ " + ".".join(_HTTP_CLIENT_2X) + ")",
        ".".join(_STREAMABLE_HTTP_1X),
    ),
    "stdio_factory": (".".join(_STDIO),),
    "sse_factory": (".".join(_SSE),),
    "server_class": (".".join(_SERVER_2X), ".".join(_SERVER_1X)),
    "context_class": (".".join(_CONTEXT_2X), ".".join(_CONTEXT_1X)),
    "lowlevel_api": (".".join(_LOWLEVEL) + " (on_call_tool= | @call_tool())",),
}


class SdkLayoutError(ImportError):
    """The installed ``mcp`` exposes none of the probed names for a seam slot."""


def _resolve(module: str, name: str) -> Any:
    """``module.name`` or ``None`` when the module or the attribute is absent.

    Any ImportError counts as absent -- including mcp 2.x's
    ``mcp.server.fastmcp``, which exists only to raise ModuleNotFoundError."""
    try:
        mod = importlib.import_module(module)
    except ImportError:
        return None
    return getattr(mod, name, None)


@dataclass(frozen=True)
class SdkLayout:
    """What the probe found. ``*_api`` names the API it matched (never a version)."""

    streamable_http_api: Optional[str]
    streamable_http_factory: Optional[Callable[..., Any]]
    http_client_factory: Optional[Callable[..., Any]]  # only with the 2.x factory
    stdio_factory: Optional[Callable[..., Any]]
    sse_factory: Optional[Callable[..., Any]]
    server_api: Optional[str]
    server_class: Optional[type]
    context_class: Optional[type]
    lowlevel_api: Optional[str]  # "on_handlers" (2.x) | "decorators" (1.x)

    def require(self, slot: str) -> Any:
        value = getattr(self, slot)
        if value is None:
            tried = ", ".join(_TRIED.get(slot, (slot,)))
            raise SdkLayoutError(
                f"the installed mcp SDK exposes no {slot.replace('_', ' ')} "
                f"(probed, in order: {tried})")
        return value


def _lowlevel_api(server_cls: Any) -> Optional[str]:
    if server_cls is None:
        return None
    try:
        params = inspect.signature(server_cls.__init__).parameters
    except (TypeError, ValueError):
        params = {}
    if "on_call_tool" in params:  # 2.x name first
        return "on_handlers"
    if callable(getattr(server_cls, "call_tool", None)):
        return "decorators"
    return None


def _probe() -> SdkLayout:
    """Feature-detect every seam slot, 2.x names first. Never raises."""
    factory_2x = _resolve(*_STREAMABLE_HTTP_2X)
    client_2x = _resolve(*_HTTP_CLIENT_2X)
    if callable(factory_2x) and callable(client_2x):
        http_api, http_factory, http_client = "streamable_http_client", factory_2x, client_2x
    else:
        factory_1x = _resolve(*_STREAMABLE_HTTP_1X)
        if callable(factory_1x):
            http_api, http_factory, http_client = "streamablehttp_client", factory_1x, None
        else:
            http_api, http_factory, http_client = None, None, None

    server_2x, context_2x = _resolve(*_SERVER_2X), _resolve(*_CONTEXT_2X)
    if server_2x is not None and context_2x is not None:
        server_api, server_cls, context_cls = "MCPServer", server_2x, context_2x
    else:
        server_1x, context_1x = _resolve(*_SERVER_1X), _resolve(*_CONTEXT_1X)
        if server_1x is not None and context_1x is not None:
            server_api, server_cls, context_cls = "FastMCP", server_1x, context_1x
        else:
            server_api, server_cls, context_cls = None, None, None

    return SdkLayout(
        streamable_http_api=http_api,
        streamable_http_factory=http_factory,
        http_client_factory=http_client,
        stdio_factory=_resolve(*_STDIO),
        sse_factory=_resolve(*_SSE),
        server_api=server_api,
        server_class=server_cls,
        context_class=context_cls,
        lowlevel_api=_lowlevel_api(_resolve(*_LOWLEVEL)),
    )


# THE probe -- once, at seam import.
LAYOUT: SdkLayout = _probe()


@asynccontextmanager
async def open_streamable_http(url: str, *, headers: Optional[Dict[str, str]] = None,
                               auth: Any = None, layout: Optional[SdkLayout] = None):
    """Open a streamable-HTTP transport with the factory the probe selected.

    Yields the transport's stream tuple ((read, write) on 2.x; (read, write,
    get_session_id) on 1.x) -- callers take the first two. Timeouts are the
    SDK defaults on both paths (30 s, 300 s read), as before the port."""
    lay = layout or LAYOUT
    factory = lay.require("streamable_http_factory")
    hdrs = dict(headers or {})
    if lay.http_client_factory is not None:
        client_kwargs: Dict[str, Any] = {"headers": hdrs}
        if auth is not None:
            client_kwargs["auth"] = auth
        async with lay.http_client_factory(**client_kwargs) as http_client:
            async with factory(url, http_client=http_client) as streams:
                yield streams
        return
    legacy_kwargs: Dict[str, Any] = {"url": url, "headers": hdrs}
    if auth is not None:
        legacy_kwargs["auth"] = auth
    async with factory(**legacy_kwargs) as streams:
        yield streams


def open_stdio(params: Any, *, layout: Optional[SdkLayout] = None):
    """The stdio transport async-cm for ``StdioServerParameters``."""
    return (layout or LAYOUT).require("stdio_factory")(params)


def open_sse(url: str, *, headers: Optional[Dict[str, str]] = None,
             layout: Optional[SdkLayout] = None):
    """The SSE transport async-cm."""
    return (layout or LAYOUT).require("sse_factory")(url=url, headers=dict(headers or {}))
