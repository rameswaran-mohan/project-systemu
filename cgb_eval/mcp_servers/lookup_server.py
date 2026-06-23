"""Hermetic 'lookup' MCP server for the CGB MCP family. Spawned over stdio.

Exposes a single read-only tool, ``resolve_code``, that returns an UNGUESSABLE
code for a key (see ``_lookup_logic.resolve_code``). The agent has no local tool
for this, so the only way to produce the correct codes is to attach this server
(``REQUEST_HARNESS kind=mcp``) and call the tool -- which is exactly the
attachment capability the MCP family tests.

Run as a script: ``python -m cgb_eval.mcp_servers.lookup_server`` (or by path).
"""
from mcp.server.fastmcp import FastMCP

# Import the shared pure logic. Support both "run as module" and "run by file
# path" (systemu's stdio transport spawns `python <abs path>`), where the package
# context may be absent.
try:  # package context (python -m ...)
    from cgb_eval.mcp_servers._lookup_logic import resolve_code as _resolve
except Exception:  # spawned by bare path: add repo root to sys.path
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from cgb_eval.mcp_servers._lookup_logic import resolve_code as _resolve

mcp = FastMCP("lookup")


@mcp.tool(annotations={"readOnlyHint": True})
def resolve_code(key: str) -> str:
    """Resolve the canonical lookup code for ``key`` from the lookup service.

    Read-only. The code is not derivable without this service.
    """
    return _resolve(key)


if __name__ == "__main__":
    mcp.run(transport="stdio")
