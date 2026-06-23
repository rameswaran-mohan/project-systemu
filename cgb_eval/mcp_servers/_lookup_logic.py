"""Pure, dependency-free lookup logic shared by the MCP server AND the oracle.

The mapping is DETERMINISTIC (reproducible runs, and the oracle can recompute the
expected answer) but UNGUESSABLE from the goal alone: the agent never sees this
code (it runs inside the server subprocess), so it cannot fabricate the codes --
it must attach the server and call the tool. This mirrors the COMPUTE family's
unguessable-token trick and the TOOL family's "an LLM cannot fake the hash".

Kept import-light (no ``mcp`` SDK) so the oracle can import ``resolve_code``
without pulling in FastMCP.
"""
from __future__ import annotations


def resolve_code(key: str) -> str:
    """Deterministic, unguessable lookup code for ``key``.

    A character-sum folded through a fixed affine transform. Stable across
    processes (no ``hash()`` / PYTHONHASHSEED dependence), so the spawned server
    and the in-process oracle agree exactly.
    """
    acc = 0
    for i, ch in enumerate(key):
        acc = (acc * 131 + ord(ch) + i) % 1_000_000_007
    return f"LK-{(acc * 48611 + 97) % 1_000_000:06d}"
