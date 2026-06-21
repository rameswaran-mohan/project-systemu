"""v0.9.34 P0 (H3): contextvars carrier for the per-run MCP session id.

ShadowRuntime.execute() sets this from the run's ``execution_id`` at the top of
a run; dispatch._mcp_handler (and, from P2, registry_bridge._make_handler) read
it at v2-dispatch time to scope "Trust for session". Resolving the id from the
active ExecutionContext — NOT from an LLM-supplied tool kwarg — is the security
property: a prompt cannot forge a cross-run trust grant. Mirrors
chat_submission_ctx.py so the set/reset discipline is identical.
"""
from __future__ import annotations

import contextvars
from typing import Optional, Any

_mcp_session_id_var: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "mcp_session_id", default=None
)


def current_mcp_session_id() -> Optional[str]:
    """Return the active run's MCP session id (or None outside a run)."""
    return _mcp_session_id_var.get()


def set_mcp_session_id(value: Optional[str], *, reset_token: Any = None):
    """Set or reset the MCP session id. Returns a token usable to reset."""
    if reset_token is not None:
        _mcp_session_id_var.reset(reset_token)
        return None
    return _mcp_session_id_var.set(value)
