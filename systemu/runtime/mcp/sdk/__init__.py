"""v0.9.36 P2 — isolation boundary for ALL official-`mcp`-SDK use.

Nothing outside this package may `import mcp`. The SDK is lazy-imported INSIDE
functions here (never at module import — v0.9.33 lesson: no import-time side
effects, never connect at import).

Confirmed SDK surface (from the Task 2 spike against mcp==1.26.0 — UPDATE if your
installed version differs):
  - mcp.ClientSession, mcp.StdioServerParameters
  - mcp.client.stdio.stdio_client            -> async cm yielding (read, write)
  - mcp.client.streamable_http.streamablehttp_client -> (read, write, get_session_id)
  - mcp.client.sse.sse_client                -> async cm yielding (read, write)
  - mcp.server.fastmcp.FastMCP               -> hermetic reference server style
  - ClientSession(read_stream, write_stream, *, read_timeout_seconds=None,
        sampling_callback=None, elicitation_callback=None, ...) — positional
        streams + keyword callbacks (the elicitation/sampling seams).
  - session.initialize(); session.list_tools() -> .tools[*].{name,description,
        inputSchema, annotations.readOnlyHint/destructiveHint}
  - session.call_tool(name, arguments) -> .content / .isError / .structuredContent
"""
