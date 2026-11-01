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

import functools
import importlib
import inspect
import threading
from contextlib import asynccontextmanager
from contextvars import ContextVar
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


# --- connect-time pinning (R-A11b-1) -------------------------------------------
#
# The SSRF guard resolves a remote MCP host ONCE and checks every address. If
# the SDK's HTTP client then resolved the name again at connect time, a DNS
# answer that changed in between (rebinding) would reach whatever it now says.
# So a guarded session gets an HTTP client whose TRANSPORT dials the checked
# address while the URL, the Host header and TLS (SNI + certificate
# verification, via the ``sni_hostname`` request extension) keep the name; every
# other host a request goes to (a followed redirect, an auth server) is vetted
# the same way and pinned too (``egress.PinnedRoutes``). Environment proxies
# never take such a session (a proxy resolves the name itself, which would void
# the pin): httpx and httpx2 mount env proxies only when NO transport is
# supplied (``allow_env_proxies = trust_env and transport is None``) and the
# pinned client always supplies one -- pinned by test on every line. The client
# is also built with ``trust_env=False``, which keeps ~/.netrc credentials off
# its requests; that is hygiene, not the proxy fence.
#
# How the client reaches the SDK, per line (all feature-detected):
#   * streamable HTTP, 2.x name: ``streamable_http_client(url, http_client=...)``
#     -- the pinned client is passed in directly;
#   * streamable HTTP, 1.x name and SSE on both lines: the transport takes an
#     ``httpx_client_factory`` -- a pinned factory is passed in.
# The SDK still OWNS the client settings: the pinned client copies timeout,
# headers, redirect policy and auth from a client built by the SDK's own
# factory (``mcp.shared._httpx_utils.create_mcp_http_client`` or the
# transport's default ``httpx_client_factory``), in the SDK's own HTTP library
# (httpx on 1.x, httpx2 on 2.x). Verified pinned on mcp 1.28, 1.30 and 2.3.
#
# RESIDUAL, stated rather than hidden: if an installed transport offers neither
# hook (no ``http_client`` / ``httpx_client_factory`` parameter), the session is
# opened UNPINNED -- still under the resolve-and-check verdict -- and a warning
# is logged naming the transport. An operator-ALLOWED endpoint is dialled by name
# on every line: the operator vouched for the name, there is no checked address.
_DEFAULT_PORTS = {"http": 80, "https": 443}
_PINNED_CLASSES: Dict[str, type] = {}
_HTTPX_UTILS = ("mcp.shared._httpx_utils", "create_mcp_http_client")


def _library_of(client: Any) -> Any:
    """The HTTP library module a client object comes from (httpx / httpx2)."""
    return importlib.import_module(type(client).__module__.split(".")[0])


def sdk_http_library() -> Any:
    """The HTTP library the installed SDK builds its clients with."""
    factory = _resolve(*_HTTPX_UTILS)
    if callable(factory):
        return _library_of(factory())
    return importlib.import_module("httpx")


def make_pinned_transport(lib: Any, routes: Any, *, inner: Any = None) -> Any:
    """An ``AsyncBaseTransport`` of ``lib`` that dials only routed addresses.

    ``routes`` is an ``egress.PinnedRoutes``; ``inner`` (default: a fresh
    ``lib.AsyncHTTPTransport()``) does the actual I/O."""
    cls = _PINNED_CLASSES.get(lib.__name__)
    if cls is None:
        cls = _build_pinned_class(lib)
        _PINNED_CLASSES[lib.__name__] = cls
    return cls(routes, inner if inner is not None else lib.AsyncHTTPTransport())


def _build_pinned_class(lib: Any) -> type:
    class PinnedTransport(lib.AsyncBaseTransport):
        def __init__(self, routes: Any, inner: Any) -> None:
            self._routes = routes
            self._inner = inner

        async def handle_async_request(self, request: Any) -> Any:
            url = request.url
            scheme = str(url.scheme or "").lower()
            host = str(url.host or "")
            port = url.port or _DEFAULT_PORTS.get(scheme)
            addresses = self._routes.cached(host, port)
            if addresses is None:
                import anyio
                try:
                    addresses = await anyio.to_thread.run_sync(
                        self._routes.route, scheme, host, port)
                except PermissionError as exc:
                    raise lib.ConnectError(str(exc), request=request) from exc
            if not addresses:  # an operator-allowed endpoint: dialled by name
                return await self._inner.handle_async_request(request)
            last: Optional[BaseException] = None
            for address in addresses:
                pinned = lib.Request(
                    request.method, url.copy_with(host=address),
                    headers=request.headers, stream=request.stream,
                    extensions={**request.extensions, "sni_hostname": host})
                try:
                    return await self._inner.handle_async_request(pinned)
                except (lib.ConnectError, lib.ConnectTimeout) as exc:
                    last = exc  # next CHECKED address; never a fresh resolution
            assert last is not None
            raise last

        async def aclose(self) -> None:
            await self._inner.aclose()

    PinnedTransport.__name__ = PinnedTransport.__qualname__ = f"Pinned{lib.__name__}Transport"
    return PinnedTransport


def _pinned_client_factory(sdk_factory: Callable[..., Any], routes: Any):
    """An ``McpHttpClientFactory`` whose clients use the pinned transport."""
    def factory(headers: Any = None, timeout: Any = None, auth: Any = None) -> Any:
        probe_kwargs: Dict[str, Any] = {"headers": headers}
        if timeout is not None:
            probe_kwargs["timeout"] = timeout
        if auth is not None:
            probe_kwargs["auth"] = auth
        probe = sdk_factory(**probe_kwargs)   # the SDK's own settings, never used for I/O
        lib = _library_of(probe)
        kwargs: Dict[str, Any] = {
            "headers": probe.headers, "timeout": probe.timeout,
            "follow_redirects": probe.follow_redirects,
            "max_redirects": probe.max_redirects,
            "trust_env": False,
            "transport": make_pinned_transport(lib, routes),
        }
        if auth is not None:
            kwargs["auth"] = auth
        return lib.AsyncClient(**kwargs)
    return factory


def _default_client_factory(transport_factory: Any) -> Optional[Callable[..., Any]]:
    """The ``httpx_client_factory`` default a transport declares, if any."""
    try:
        param = inspect.signature(transport_factory).parameters.get("httpx_client_factory")
    except (TypeError, ValueError):
        return None
    if param is None or not callable(param.default):
        return None
    return param.default


def _unpinned(kind: str) -> None:
    import logging
    logging.getLogger(__name__).warning(
        "[MCP] the installed %s transport offers no HTTP-client hook: the session is "
        "SSRF-checked but NOT pinned to the checked address", kind)


@asynccontextmanager
async def open_streamable_http(url: str, *, headers: Optional[Dict[str, str]] = None,
                               auth: Any = None, layout: Optional[SdkLayout] = None,
                               pin: Any = None):
    """Open a streamable-HTTP transport with the factory the probe selected.

    Yields the transport's stream tuple ((read, write) on 2.x; (read, write,
    get_session_id) on 1.x) -- callers take the first two. Timeouts are the
    SDK defaults on both paths (30 s, 300 s read), as before the port.
    ``pin`` (an ``egress.PinnedRoutes``) makes every connection dial the
    checked address (see "connect-time pinning" above)."""
    lay = layout or LAYOUT
    factory = lay.require("streamable_http_factory")
    hdrs = dict(headers or {})
    if lay.http_client_factory is not None:
        client_kwargs: Dict[str, Any] = {"headers": hdrs}
        if auth is not None:
            client_kwargs["auth"] = auth
        make = (lay.http_client_factory if pin is None
                else _pinned_client_factory(lay.http_client_factory, pin))
        async with make(**client_kwargs) as http_client:
            async with factory(url, http_client=http_client) as streams:
                yield streams
        return
    legacy_kwargs: Dict[str, Any] = {"url": url, "headers": hdrs}
    if auth is not None:
        legacy_kwargs["auth"] = auth
    if pin is not None:
        default = _default_client_factory(factory)
        if default is None:
            _unpinned("streamable HTTP")
        else:
            legacy_kwargs["httpx_client_factory"] = _pinned_client_factory(default, pin)
    async with factory(**legacy_kwargs) as streams:
        yield streams


# --- the stdio spawn witness (Defect 2: name a server's exit code) ------------
#
# Neither line exposes the spawned server process: ``stdio_client`` keeps it in
# a local. To report "the server exited with code N" the seam wraps the ONE
# function both lines call to spawn it -- ``mcp.client.stdio.
# _create_platform_compatible_process`` (a private name, so it is FEATURE-
# DETECTED like every other slot here, never assumed). The wrapper is
# transparent: it returns the very process object the SDK made, and only when a
# caller has opened a sink in the CURRENT task context (a ContextVar, so
# concurrent sessions never see each other's process) does it also record it.
# If a future SDK renames the function, nothing is wrapped and the failure
# reason honestly says the exit code is unavailable.
_STDIO_SPAWN = ("mcp.client.stdio", "_create_platform_compatible_process")
_SPAWN_MARK = "__systemu_spawn_witness__"
_SPAWN_SINK: "ContextVar[Optional[list]]" = ContextVar(
    "systemu_mcp_stdio_spawn_sink", default=None)
_SPAWN_LOCK = threading.Lock()


def _install_spawn_witness() -> bool:
    """Wrap the SDK's stdio spawn function once. True when the witness is live."""
    try:
        mod = importlib.import_module(_STDIO_SPAWN[0])
    except ImportError:
        return False
    with _SPAWN_LOCK:
        current = getattr(mod, _STDIO_SPAWN[1], None)
        if current is None or not callable(current):
            return False
        if getattr(current, _SPAWN_MARK, False):
            return True

        @functools.wraps(current)
        async def _witnessed(*args: Any, **kwargs: Any) -> Any:
            process = await current(*args, **kwargs)
            sink = _SPAWN_SINK.get()
            if sink is not None:
                sink.append(process)
            return process

        setattr(_witnessed, _SPAWN_MARK, True)
        setattr(mod, _STDIO_SPAWN[1], _witnessed)
        return True


@asynccontextmanager
async def open_stdio(params: Any, *, layout: Optional[SdkLayout] = None,
                     errlog: Any = None, spawned: Optional[list] = None):
    """The stdio transport for ``StdioServerParameters``.

    ``errlog`` (a real file) receives the server's stderr instead of this
    process's stderr. ``spawned`` (a list) receives the server process object
    once it is spawned, through the spawn witness above."""
    factory = (layout or LAYOUT).require("stdio_factory")
    kwargs: Dict[str, Any] = {} if errlog is None else {"errlog": errlog}
    token = None
    if spawned is not None and _install_spawn_witness():
        token = _SPAWN_SINK.set(spawned)
    try:
        async with factory(params, **kwargs) as streams:
            yield streams
    finally:
        if token is not None:
            try:
                _SPAWN_SINK.reset(token)
            except ValueError:  # finalised in another context: nothing to undo
                pass


def open_sse(url: str, *, headers: Optional[Dict[str, str]] = None,
             layout: Optional[SdkLayout] = None, pin: Any = None):
    """The SSE transport async-cm. ``pin`` (an ``egress.PinnedRoutes``) makes
    every connection dial the checked address (see "connect-time pinning")."""
    factory = (layout or LAYOUT).require("sse_factory")
    kwargs: Dict[str, Any] = {"url": url, "headers": dict(headers or {})}
    if pin is not None:
        default = _default_client_factory(factory)
        if default is None:
            _unpinned("SSE")
        else:
            kwargs["httpx_client_factory"] = _pinned_client_factory(default, pin)
    return factory(**kwargs)
