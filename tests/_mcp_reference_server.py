"""Hermetic reference MCP server for the MCP client tests. Spawned by
stdio_client (default) or served over a real socket. Declares tools with explicit
annotations so the action-gate risk tiers (read-only / action / destructive) are
exercisable:

    echo          - read-only (readOnlyHint=True)
    create_note   - action, non-destructive
    delete_thing  - destructive (destructiveHint=True)
    fail_always   - raises: pins the tool-error envelope (isError on the wire)
    ask_name      - asks the CLIENT for input (server-initiated elicitation)

Runs on BOTH official-SDK lines (D-MCP2): the server class comes from systemu's
ONE SDK seam, ``systemu.runtime.mcp.sdk.compat`` -- mcp 2.x ``MCPServer`` probed
first, then mcp 1.x ``FastMCP``. Same decorator surface on both.

Run as a script:
    python tests/_mcp_reference_server.py                       # stdio
    python tests/_mcp_reference_server.py streamable-http PORT  # http://127.0.0.1:PORT/mcp
    python tests/_mcp_reference_server.py sse PORT              # http://127.0.0.1:PORT/sse
"""
import sys
from pathlib import Path

# Spawned as a bare script, sys.path[0] is tests/. Put THIS checkout first so
# the seam that picks the server class is the one under test, never a copy of
# systemu installed elsewhere.
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pydantic import BaseModel  # noqa: E402

from systemu.runtime.mcp.sdk.compat import LAYOUT  # noqa: E402

Context = LAYOUT.require("context_class")
mcp = LAYOUT.require("server_class")("reference")


@mcp.tool(annotations={"readOnlyHint": True})
def echo(text: str) -> str:
    """Echo the input text back. Read-only."""
    return f"echo: {text}"


@mcp.tool(annotations={"destructiveHint": True})
def delete_thing(thing_id: str) -> str:
    """Delete a thing by id. Destructive and irreversible."""
    return f"deleted {thing_id}"


@mcp.tool()
def create_note(title: str, body: str = "") -> str:
    """Create a note. Action, non-destructive. `title` is required; `body` optional."""
    return f"created note {title!r}"


@mcp.tool()
def fail_always() -> str:
    """Always fails. The SDK turns the exception into an isError result."""
    raise RuntimeError("fail_always: deliberate tool failure")


class _Name(BaseModel):
    name: str


@mcp.tool()
async def ask_name(ctx: Context) -> str:
    """Ask the client for a name; report the action and the value received."""
    res = await ctx.elicit(message="What is your name?", schema=_Name)
    data = getattr(res, "data", None)
    return f"{res.action}:{getattr(data, 'name', None)}"


def _serve_http(transport: str, port: int) -> None:
    import uvicorn
    app = mcp.streamable_http_app() if transport == "streamable-http" else mcp.sse_app()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] in ("streamable-http", "sse"):
        _serve_http(sys.argv[1], int(sys.argv[2]))
    else:
        mcp.run(transport="stdio")
