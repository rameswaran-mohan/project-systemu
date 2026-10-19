"""v0.9.36 P2 — isolation boundary for ALL official-`mcp`-SDK use.

Nothing outside this package may `import mcp`. The SDK is lazy-imported INSIDE
functions here (never at module import — v0.9.33 lesson: no import-time side
effects, never connect at import).

SDK lines: BOTH mcp 1.x and mcp 2.x are supported (D-MCP2). Every name that
differs between the lines is resolved ONCE, by feature detection (2.x names
first, then 1.x names), in ``compat.py`` -- the one place to look when the SDK
moves again. SDK models are read by WIRE (camelCase) name through
``schema_map.wire_field``: 1.x models expose ``isError``, 2.x models expose
``is_error`` behind a camelCase alias.

Confirmed SDK surface, the same on mcp 2.3.0 and mcp 1.30.0:
  - mcp.ClientSession, mcp.StdioServerParameters
  - mcp.client.stdio.stdio_client            -> async cm yielding (read, write)
  - mcp.client.sse.sse_client                -> async cm yielding (read, write)
  - streamable HTTP via compat.open_streamable_http (2.x name
        streamable_http_client(url, http_client=...); 1.x name
        streamablehttp_client(url=, headers=), removed in 2.x)
  - hermetic reference servers via compat's server_class (2.x MCPServer;
        1.x FastMCP -- on 2.x mcp.server.fastmcp only raises)
  - ClientSession(read_stream, write_stream, *, read_timeout_seconds=None,
        sampling_callback=None, elicitation_callback=None, ...) — positional
        streams + keyword callbacks (the elicitation/sampling seams).
  - session.initialize(); session.list_tools() -> .tools[*] with wire fields
        name, description, inputSchema, annotations.readOnlyHint/destructiveHint
  - session.call_tool(name, arguments) -> wire fields content / isError /
        structuredContent
"""
