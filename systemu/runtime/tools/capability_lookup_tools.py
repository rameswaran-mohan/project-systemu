"""R-CAP1 · CAP-4c -- ``find_tools`` as a REGISTERED tool.

The selection view (``runtime.capability_view``) shows a prompt only its top-K
tools in full and a one-line index for the rest. That trim is legitimate only
because the agent can always ask for more: this is the asking. It searches the
COMPLETE store -- every Tool-catalog row whatever its state, the enabled MCP
tools, and the code-registered tools -- and returns each hit with its state and
reason (never-subtract, R9(b)).

It is not an action tool: a deterministic read of local stores. No model call,
and as an ordinary ``TOOL_CALL`` it costs no harness-request budget. It never
changes what may be dispatched.

Lanes call it with their own dispatch set so each hit says whether it is
callable THERE (the quick lane and the runtime decision loop both intercept the
name before any sandbox dispatch). This registered handler is the lane-less form:
it opens the vault the way every v2 handler does and reports ``callable here:
unknown``.
"""
from __future__ import annotations

from typing import Any, Dict

from systemu.runtime.capability_view import (FIND_TOOLS_DESCRIPTION, FIND_TOOLS_NAME,
                                             FIND_TOOLS_SCHEMA, find_tools_tool_result)
from systemu.runtime.tool_registry_v2 import registry


def _open_vault():
    """The active vault (a seam so a test can drive the handler without a
    configured environment)."""
    from sharing_on.config import Config
    from systemu.vault.factory import open_vault
    return open_vault(Config.from_env())


def find_tools_handler(**kwargs: Any) -> Dict[str, Any]:
    """Never raises: every failure is an honest failure result."""
    try:
        vault = _open_vault()
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"no vault: {exc}"}
    return find_tools_tool_result(vault, {k: v for k, v in kwargs.items()
                                          if k in ("query", "limit")})


# ── Module-level registration (AST-scan discovery picks this up) ──

registry.register(
    name=FIND_TOOLS_NAME, toolset="capability",
    schema=FIND_TOOLS_SCHEMA, handler=find_tools_handler,
    description=FIND_TOOLS_DESCRIPTION,
    is_action_tool=False,
    max_result_size_chars=12_000,
)
