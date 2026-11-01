"""A hermetic MCP server whose tools exercise the P21 effect classification (stdio).

Each tool is a shape the MCP gate card must classify from what the SERVER declares:

    transfer_funds  - money by NAME (and schema), no annotations
    delete_records  - destructive by name AND destructiveHint=True
    reset_ledger    - destructive ONLY by destructiveHint=True (no name signal)
    remove_user     - CLAIMS readOnlyHint=True while being a delete by name
    sync_data       - no annotations at all (the UNKNOWN band)
    lookup_status   - honestly read-only (readOnlyHint=True, nothing contradicts it)
    settle_account  - money by NAME alone ("settle", the money-move net's token);
                      not a destructive name, so the chat lane's pre-filter passes it

Runs on both SDK lines through systemu's one SDK seam (mirrors
``tests/_mcp_zero_tool_server.py``).

    python tests/_mcp_effects_server.py
"""
import sys
from pathlib import Path

# Spawned as a bare script, sys.path[0] is tests/. Put THIS checkout first so the
# seam under test is the one that picks the server class.
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from systemu.runtime.mcp.sdk.compat import LAYOUT  # noqa: E402

mcp = LAYOUT.require("server_class")("effects")


@mcp.tool()
def transfer_funds(to_account: str, amount: float) -> str:
    """Move money to another account."""
    return f"moved {amount} to {to_account}"


@mcp.tool(annotations={"destructiveHint": True})
def delete_records(record_ids: str) -> str:
    """Delete records permanently."""
    return f"deleted {record_ids}"


@mcp.tool(annotations={"destructiveHint": True})
def reset_ledger(ledger_id: str) -> str:
    """Reset a ledger to empty."""
    return f"reset {ledger_id}"


@mcp.tool(annotations={"readOnlyHint": True})
def remove_user(user_id: str) -> str:
    """Claims to be read-only. It is not."""
    return f"removed {user_id}"


@mcp.tool()
def sync_data(source: str) -> str:
    """Sync data from a source. Declares nothing about its effects."""
    return f"synced {source}"


@mcp.tool()
def settle_account(account_id: str) -> str:
    """Settle an account's outstanding balance."""
    return f"settled {account_id}"


@mcp.tool(annotations={"readOnlyHint": True})
def lookup_status(item: str) -> str:
    """Look up an item's status. Read-only."""
    return f"status of {item}: ok"


if __name__ == "__main__":
    mcp.run(transport="stdio")
