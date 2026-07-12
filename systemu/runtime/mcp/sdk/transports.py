"""v0.9.36 P2 — transport construction for the MCP client (SDK-isolated).

The ONLY module that imports transport constructors. Everything is lazy-imported
INSIDE functions — never at module import (no import-time SDK load, never connect
at import). Declares client capabilities {elicitation, sampling} so connected
servers may use them; routes server elicitation through the supplied callback
(the P1 surface seam — wired in Task 13).

A transport spec is a plain dict:
    stdio : {"transport":"stdio","command":str,"args":[str],"env":{str:str}}
    http  : {"transport":"http"|"streamable-http","url":str,"headers":{...}?}
    sse   : {"transport":"sse","url":str,"headers":{...}?}

Confirmed against mcp==1.26.0:
  - ClientSession(read_stream, write_stream, *, read_timeout_seconds=None,
        sampling_callback=None, elicitation_callback=None, ...) — positional
        streams + keyword callbacks.
  - stdio_client(StdioServerParameters)         -> async cm yielding (read, write)
  - streamablehttp_client(url=, headers=)       -> async cm yielding (read, write, get_session_id)
  - sse_client(url=, headers=)                  -> async cm yielding (read, write)
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Dict, Optional

# Default client capabilities advertised to every server. elicitation has both
# form and url modes; sampling is DECLARED in P2 (handler lands in P4) so
# negotiation works and the surface is forward-compatible.
CLIENT_CAPABILITIES = {"elicitation": {"form": {}, "url": {}}, "sampling": {}}

_VALID_TRANSPORTS = {"stdio", "http", "streamable-http", "sse"}

# Type of the elicitation callback the manager injects (Task 13 supplies the
# real one; default returns a "decline" so an un-wired client is fail-closed,
# never fabricating a value).
ElicitCallback = Callable[[Any, Any], Awaitable[Any]]


def build_stdio_params(spec: Dict[str, Any]):
    """Construct StdioServerParameters from a spec. ``env`` is the EXACT child
    env (no merge with os.environ) — only operator-approved keys reach the
    child (credential-leak defence)."""
    from mcp import StdioServerParameters
    return StdioServerParameters(
        command=str(spec.get("command") or ""),
        args=list(spec.get("args") or []),
        env=dict(spec.get("env") or {}),
    )


async def _default_elicitation_callback(context, params):
    """Fail-closed default: decline. Real impl injected by the manager (Task 13)."""
    # Shape must match the SDK's ElicitResult; "decline" is the safe default.
    return {"action": "decline"}


@asynccontextmanager
async def open_session(spec: Dict[str, Any], *,
                       elicitation_callback: Optional[ElicitCallback] = None,
                       sampling_callback: Optional[ElicitCallback] = None,
                       init_timeout: float = 30.0,
                       classification_trusted: bool = True):
    """Yield a live, initialised ClientSession for ``spec``.

    Lazy-imports the SDK. Declares client capabilities and wires the
    elicitation callback. Caller MUST use this as `async with`.

    ``sampling_callback`` EXISTS from P2 (contract-pinned) but is left ``None``
    here — P4 fills the sampling injection without re-editing this constructor.

    R-A14a §15.1(c) / IMPL-13 / DEC-1 — ``classification_trusted`` gates the STDIO
    launch. A stdio transport SPAWNS an MCP server subprocess; pre-S2 there is NO
    OS-kernel egress jail, so a REGISTRY / untrusted stdio server would egress
    unrestricted. The DEFAULT is ``True`` (an OPERATOR-CONNECTED server — the
    operator vouched by adding it in Settings → Connectors — and every existing
    caller). A future R-A11 registry/discovery caller passes
    ``classification_trusted=False``; such a launch is REFUSED pre-jail (before
    the SDK import + the transport constructor — never launched-then-denied) with
    an ``egress_enforcer_unavailable``-class error until S2 ships. Remote
    (http/sse) transports do not spawn a child and are unaffected here (their
    SSRF/TLS guard lives in the ConnectionManager).
    """
    transport = str(spec.get("transport") or "").lower()
    if transport not in _VALID_TRANSPORTS:
        raise ValueError(f"unknown MCP transport: {transport!r}")

    # §15.1(c): refuse a registry/untrusted stdio LAUNCH pre-jail (fail-closed).
    if transport == "stdio" and not classification_trusted:
        from systemu.runtime.action_governance import (
            _egress_enforcer_available, EGRESS_ENFORCER_UNAVAILABLE_STDIO)
        if not _egress_enforcer_available():
            raise PermissionError(EGRESS_ENFORCER_UNAVAILABLE_STDIO)

    from mcp import ClientSession  # lazy

    cb = elicitation_callback or _default_elicitation_callback

    if transport == "stdio":
        from mcp.client.stdio import stdio_client
        params = build_stdio_params(spec)
        client_cm = stdio_client(params)
    elif transport in ("http", "streamable-http"):
        from mcp.client.streamable_http import streamablehttp_client
        client_cm = streamablehttp_client(
            url=str(spec.get("url") or ""),
            headers=dict(spec.get("headers") or {}),
        )
    else:  # sse
        from mcp.client.sse import sse_client
        client_cm = sse_client(
            url=str(spec.get("url") or ""),
            headers=dict(spec.get("headers") or {}),
        )

    async with client_cm as streams:
        # stdio/sse yield (read, write); streamable-http yields
        # (read, write, get_session_id). Take the first two either way.
        read, write = streams[0], streams[1]
        # mcp==1.26.0 derives client capabilities from which callbacks are
        # passed: elicitation_callback advertises {elicitation}, sampling_callback
        # advertises {sampling}. sampling_callback is forwarded ONLY when supplied
        # (None in P2 — P4 fills it). Build kwargs so an un-set sampling slot
        # stays absent (the SDK then does not advertise sampling).
        _session_kwargs = {"elicitation_callback": cb}
        if sampling_callback is not None:
            _session_kwargs["sampling_callback"] = sampling_callback
        async with ClientSession(
            read, write, **_session_kwargs,
        ) as session:
            import anyio
            with anyio.fail_after(init_timeout):
                await session.initialize()
            yield session


def make_sampling_callback(*, config, tier: int = 2, on_gate=None):
    """Return the ClientSession ``sampling_callback`` that answers a server's
    sampling/createMessage by routing through systemu's llm_router. SDK types
    are converted to plain dicts so the routing core stays MCP-free.

    ``on_gate`` is threaded straight into ``route_sampling_request``. The hermetic
    TEST builds the callback with ``on_gate=None`` (allow). PRODUCTION builds it via
    ``shadow_runtime.build_sampling_callback`` (Task 2b / H9), which ALWAYS supplies
    a gate-backed ``on_gate`` that defaults to ASK on the floor — so the live host
    never routes a sampling request without an operator gate.

    Confirmed against mcp==1.26.0: the callback is
    ``async def cb(context: RequestContext, params: CreateMessageRequestParams)``
    returning ``CreateMessageResult | ErrorData``.
    """
    from systemu.runtime.mcp.sdk import sampling as _sampling

    async def _cb(context, params):  # noqa: ANN001
        import mcp.types as t

        # SDK params -> plain dict (MCP-free core handles it). The optional
        # numeric fields are None when the server omits them — coerce to the
        # systemu defaults so the core's float()/int() casts never see None.
        _temp = getattr(params, "temperature", None)
        req = {
            "systemPrompt": getattr(params, "systemPrompt", "") or "",
            "maxTokens": getattr(params, "maxTokens", 1024) or 1024,
            "temperature": 0.3 if _temp is None else _temp,
            "messages": [
                {
                    "role": getattr(m, "role", "user"),
                    "content": {
                        "type": "text",
                        "text": getattr(getattr(m, "content", None), "text", "") or "",
                    },
                }
                for m in (getattr(params, "messages", []) or [])
            ],
        }
        result = _sampling.route_sampling_request(
            req, config=config, tier=tier, on_gate=on_gate)
        # plain dict -> SDK CreateMessageResult (confirmed fields: role, content,
        # model, stopReason).
        return t.CreateMessageResult(
            role="assistant",
            content=t.TextContent(type="text", text=result["content"]["text"]),
            model=result["model"],
            stopReason="endTurn",
        )

    return _cb


def build_test_sampling_server():
    """Hermetic in-process MCP server with one tool that asks the CLIENT to
    sample, then returns the answer. Test-only; lives here so the SDK import
    stays isolated. Confirmed against mcp==1.26.0: lowlevel ``Server`` +
    ``ServerSession.create_message(messages=[SamplingMessage], max_tokens=...)``.
    """
    import mcp.types as t
    from mcp.server.lowlevel import Server

    server = Server("p4-test-sampling-server")

    @server.list_tools()
    async def _list_tools():
        return [t.Tool(name="ask_parent", description="ask the client's model",
                       inputSchema={"type": "object",
                                    "properties": {"q": {"type": "string"}},
                                    "required": ["q"]})]

    @server.call_tool()
    async def _call_tool(name, arguments):  # noqa: ANN001
        # Ask the CLIENT for a completion (this triggers the client callback).
        ctx = server.request_context
        res = await ctx.session.create_message(
            messages=[t.SamplingMessage(
                role="user",
                content=t.TextContent(type="text", text=str(arguments.get("q", ""))),
            )],
            max_tokens=64,
        )
        answer = getattr(getattr(res, "content", None), "text", "") or ""
        return [t.TextContent(type="text", text=f"server-saw: {answer}")]

    return server
