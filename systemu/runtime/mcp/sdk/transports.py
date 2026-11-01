"""v0.9.36 P2 — transport construction for the MCP client (SDK-isolated).

Transport constructors come from the ONE SDK-line seam (``compat.LAYOUT``,
D-MCP2) -- this module never names a line-specific constructor itself.
Everything is lazy-imported INSIDE functions — never at module import (no
import-time SDK load, never connect at import). Declares client capabilities
{elicitation, sampling} so connected servers may use them; routes server
elicitation through the supplied callback
(the P1 surface seam — wired in Task 13).

A transport spec is a plain dict:
    stdio : {"transport":"stdio","command":str,"args":[str],"env":{str:str}}
    http  : {"transport":"http"|"streamable-http","url":str,"headers":{...}?}
    sse   : {"transport":"sse","url":str,"headers":{...}?}

Confirmed against mcp 2.3.0 and mcp 1.30.0 (both lines):
  - ClientSession(read_stream, write_stream, *, read_timeout_seconds=None,
        sampling_callback=None, elicitation_callback=None, ...) — positional
        streams + keyword callbacks.
  - stdio_client(StdioServerParameters)  -> async cm yielding (read, write)
  - sse_client(url=, headers=)           -> async cm yielding (read, write)
  - streamable HTTP: see compat.open_streamable_http -- (read, write) on 2.x,
        (read, write, get_session_id) on 1.x.
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


def _returncode(process: Any) -> Optional[int]:
    """The exit code of an SDK server process, or None while it runs.

    anyio ``Process`` and the 2.x win32 fallback expose ``returncode``; the 1.x
    win32 fallback only wraps a ``Popen`` as ``.popen``."""
    rc = getattr(process, "returncode", None)
    if type(rc) is int:
        return rc
    popen = getattr(process, "popen", None)
    if popen is not None:
        try:
            rc = popen.poll()
        except Exception:
            return None
        if type(rc) is int:
            return rc
    return None


class StdioWitness:
    """What a stdio session leaves behind for an honest failure reason.

    Owns a temp file the server's stderr goes to (instead of this process's
    console) and receives the spawned process from the compat spawn witness.
    ``exit_code`` is set ONLY when the server exited ON ITS OWN -- observed
    within a short grace window after the session failed and BEFORE the SDK's
    shutdown terminates it -- so a kill at teardown is never reported as the
    server's own exit code. Use as a context manager (closes the temp file)."""

    GRACE_S = 1.5

    def __init__(self, known_values: Optional[list] = None) -> None:
        import tempfile
        self.errlog = tempfile.TemporaryFile()
        self.spawned: list = []
        self.exit_code: Optional[int] = None
        self._known_values = [str(v) for v in (known_values or []) if v]

    @classmethod
    def for_spec(cls, spec: Dict[str, Any]):
        """A witness for a stdio spec; a null context (``None``) otherwise."""
        from contextlib import nullcontext
        if str(spec.get("transport") or "").lower() != "stdio":
            return nullcontext(None)
        env = spec.get("env") or {}
        return cls(known_values=list(env.values()) if isinstance(env, dict) else [])

    def __enter__(self) -> "StdioWitness":
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            self.errlog.close()
        except Exception:
            pass

    @property
    def process(self) -> Any:
        return self.spawned[-1] if self.spawned else None

    async def settle(self) -> None:
        """Record the exit code if the server is exiting on its own. Polls (never
        a blocking wait) inside a shielded, bounded scope."""
        proc = self.process
        if proc is None or self.exit_code is not None:
            return
        import anyio
        with anyio.CancelScope(shield=True):
            with anyio.move_on_after(self.GRACE_S):
                while _returncode(proc) is None:
                    await anyio.sleep(0.05)
        rc = _returncode(proc)
        if type(rc) is int:
            self.exit_code = rc

    def stderr_lines(self) -> list:
        """The last stderr lines, redacted before they are capped (DEC-31)."""
        from systemu.runtime.mcp.sdk.failure import STDERR_WINDOW_BYTES, stderr_tail
        f = self.errlog
        try:
            f.flush()
            size = f.seek(0, 2)
            start = max(0, size - STDERR_WINDOW_BYTES)
            f.seek(start)
            raw = f.read()
        except Exception:
            return []
        return stderr_tail(raw, truncated=start > 0, known_values=self._known_values)


async def _default_elicitation_callback(context, params):
    """Fail-closed default: decline. Real impl injected by the manager (Task 13)."""
    # Shape must match the SDK's ElicitResult; "decline" is the safe default.
    return {"action": "decline"}


@asynccontextmanager
async def open_session(spec: Dict[str, Any], *,
                       elicitation_callback: Optional[ElicitCallback] = None,
                       sampling_callback: Optional[ElicitCallback] = None,
                       init_timeout: float = 30.0,
                       classification_trusted: bool = True,
                       witness: Optional[StdioWitness] = None,
                       egress: Any = None):
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

    # R-A11b-1: EVERY remote session is opened under an ADMITTING SSRF verdict
    # issued for THIS url -- there is no unguarded default. The concrete type is
    # pinned before the value is read (DEC-36); a mismatch refuses before any
    # SDK import or socket. The session is then PINNED to the addresses the
    # verdict checked (compat "connect-time pinning").
    pin = None
    if transport != "stdio":
        from systemu.runtime.mcp.sdk.egress import EgressVerdict, PinnedRoutes
        if egress is None:
            raise PermissionError("remote MCP connect refused: no SSRF verdict was issued "
                                  "for this session")
        if type(egress) is not EgressVerdict or egress.admitted is not True:
            raise PermissionError("remote MCP connect refused: the SSRF verdict does not admit it")
        if egress.url != str(spec.get("url") or ""):
            raise PermissionError("remote MCP connect refused: the SSRF verdict was issued "
                                  "for a different URL")
        pin = PinnedRoutes(egress)

    # §15.1(c): refuse a registry/untrusted stdio LAUNCH pre-jail (fail-closed).
    if transport == "stdio" and not classification_trusted:
        from systemu.runtime.action_governance import (
            _egress_enforcer_available, EGRESS_ENFORCER_UNAVAILABLE_STDIO)
        if not _egress_enforcer_available():
            raise PermissionError(EGRESS_ENFORCER_UNAVAILABLE_STDIO)

    from mcp import ClientSession  # lazy
    from systemu.runtime.mcp.sdk import compat  # the ONE SDK-line seam (lazy)

    cb = elicitation_callback or _default_elicitation_callback

    if transport == "stdio":
        if witness is not None:
            client_cm = compat.open_stdio(build_stdio_params(spec),
                                          errlog=witness.errlog,
                                          spawned=witness.spawned)
        else:
            client_cm = compat.open_stdio(build_stdio_params(spec))
    elif transport in ("http", "streamable-http"):
        client_cm = compat.open_streamable_http(
            str(spec.get("url") or ""),
            headers=dict(spec.get("headers") or {}),
            pin=pin,
        )
    else:  # sse
        client_cm = compat.open_sse(
            str(spec.get("url") or ""),
            headers=dict(spec.get("headers") or {}),
            pin=pin,
        )

    async with client_cm as streams:
        # stdio/sse yield (read, write); streamable-http yields (read, write) on
        # mcp 2.x and (read, write, get_session_id) on 1.x. Take the first two.
        read, write = streams[0], streams[1]
        # mcp==1.26.0 derives client capabilities from which callbacks are
        # passed: elicitation_callback advertises {elicitation}, sampling_callback
        # advertises {sampling}. sampling_callback is forwarded ONLY when supplied
        # (None in P2 — P4 fills it). Build kwargs so an un-set sampling slot
        # stays absent (the SDK then does not advertise sampling).
        _session_kwargs = {"elicitation_callback": cb}
        if sampling_callback is not None:
            _session_kwargs["sampling_callback"] = sampling_callback
        try:
            async with ClientSession(
                read, write, **_session_kwargs,
            ) as session:
                import anyio
                with anyio.fail_after(init_timeout):
                    await session.initialize()
                yield session
        except Exception:
            # Still INSIDE the transport: the SDK has not torn the server down
            # yet, so an exit code observed now is the server's own (Defect 2).
            if witness is not None:
                await witness.settle()
            raise


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
        from systemu.runtime.mcp.sdk.schema_map import wire_field

        # SDK params -> plain dict (MCP-free core handles it). The optional
        # numeric fields are None when the server omits them — coerce to the
        # systemu defaults so the core's float()/int() casts never see None.
        # D-MCP2: read by WIRE name — on mcp 2.x the attributes are
        # system_prompt / max_tokens, and a getattr on systemPrompt / maxTokens
        # silently dropped the server's system prompt and token ceiling.
        _temp = wire_field(params, "temperature")
        req = {
            "systemPrompt": wire_field(params, "systemPrompt", "") or "",
            "maxTokens": wire_field(params, "maxTokens", 1024) or 1024,
            "temperature": 0.3 if _temp is None else _temp,
            "messages": [
                {
                    "role": wire_field(m, "role", "user"),
                    "content": {
                        "type": "text",
                        "text": wire_field(wire_field(m, "content"), "text", "") or "",
                    },
                }
                for m in (wire_field(params, "messages", []) or [])
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


TEST_SAMPLING_SYSTEM_PROMPT = "p4-test-sampling-system-prompt"
TEST_SAMPLING_MAX_TOKENS = 64


def build_test_sampling_server():
    """Hermetic in-process MCP server with one tool that asks the CLIENT to
    sample, then returns the answer. Test-only; lives here so the SDK import
    stays isolated. Lowlevel ``Server`` +
    ``ServerSession.create_message(messages=[SamplingMessage], max_tokens=...,
    system_prompt=...)`` -- the same call on both SDK lines.

    D-MCP2: handler REGISTRATION differs by line and is chosen by the ONE seam
    (``compat.LAYOUT.lowlevel_api``): mcp 2.x takes ``on_list_tools=`` /
    ``on_call_tool=`` constructor handlers ``(ctx, params)``; mcp 1.x uses the
    ``@server.list_tools()`` / ``@server.call_tool()`` decorators.
    """
    import mcp.types as t
    from mcp.server.lowlevel import Server
    from systemu.runtime.mcp.sdk import compat
    from systemu.runtime.mcp.sdk.schema_map import wire_field

    tool = t.Tool(name="ask_parent", description="ask the client's model",
                  inputSchema={"type": "object",
                               "properties": {"q": {"type": "string"}},
                               "required": ["q"]})

    async def _ask_client(session, arguments):  # noqa: ANN001
        # Ask the CLIENT for a completion (this triggers the client callback).
        res = await session.create_message(
            messages=[t.SamplingMessage(
                role="user",
                content=t.TextContent(type="text", text=str((arguments or {}).get("q", ""))),
            )],
            max_tokens=TEST_SAMPLING_MAX_TOKENS,
            system_prompt=TEST_SAMPLING_SYSTEM_PROMPT,
        )
        answer = wire_field(wire_field(res, "content"), "text", "") or ""
        return [t.TextContent(type="text", text=f"server-saw: {answer}")]

    style = compat.LAYOUT.require("lowlevel_api")
    if style == "on_handlers":  # mcp 2.x
        async def _on_list_tools(ctx, params):  # noqa: ANN001
            return t.ListToolsResult(tools=[tool])

        async def _on_call_tool(ctx, params):  # noqa: ANN001
            content = await _ask_client(ctx.session, wire_field(params, "arguments", {}))
            return t.CallToolResult(content=content)

        return Server("p4-test-sampling-server",
                      on_list_tools=_on_list_tools, on_call_tool=_on_call_tool)

    server = Server("p4-test-sampling-server")  # mcp 1.x: decorators

    @server.list_tools()
    async def _list_tools():
        return [tool]

    @server.call_tool()
    async def _call_tool(name, arguments):  # noqa: ANN001
        return await _ask_client(server.request_context.session, arguments)

    return server
