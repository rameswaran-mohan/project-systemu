"""Hermetic in-process reference MCP server for P2 tests. Spawned over stdio by
stdio_client. Declares three tools with explicit annotations so the action-gate
risk tiers (read-only / action / destructive) are exercisable:

    echo          - read-only (readOnlyHint=True)
    create_note   - action, non-destructive
    delete_thing  - destructive (destructiveHint=True)

Run as a script: `python tests/_mcp_reference_server.py`.
"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("reference")


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


if __name__ == "__main__":
    mcp.run(transport="stdio")
