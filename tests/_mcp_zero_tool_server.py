"""A hermetic MCP server that offers ZERO tools (stdio only).

It is the control for the honest-listing tests: "listed, and there are no
tools" must read as success with an empty list, which is a different fact from
"could not list". Runs on both SDK lines through systemu's one SDK seam.

    python tests/_mcp_zero_tool_server.py
"""
import sys
from pathlib import Path

# Spawned as a bare script, sys.path[0] is tests/. Put THIS checkout first so
# the seam under test is the one that picks the server class.
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from systemu.runtime.mcp.sdk.compat import LAYOUT  # noqa: E402

mcp = LAYOUT.require("server_class")("zero-tools")

if __name__ == "__main__":
    mcp.run(transport="stdio")
