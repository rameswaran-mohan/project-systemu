"""v0.9.36 P2 — bridge enabled MCP tools into the v2 registry as namespaced
`mcp__server__tool` entries.

Each entry:
  * routes its handler through P0's chokepoint (systemu.runtime.mcp.dispatch.
    call_mcp_tool) — one trust truth, one transport, one action gate;
  * carries a check_fn that re-confirms the tool is still operator-allowlisted
    (fail-closed; disabled tools vanish from the catalog automatically);
  * uses the mapped parameters_schema (real required[]);
  * sets is_action_tool from the MCP readOnlyHint (read-only => tier R, no gate).

No SDK import here — operates on the normalised dicts the manager produces.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

from systemu.runtime.tool_registry_v2 import registry

logger = logging.getLogger(__name__)

_NS_PREFIX = "mcp__"


def _slug(server: str) -> str:
    # Stable, filename-safe server slug: scheme/punct -> underscore.
    s = re.sub(r"^[a-z]+://", "", (server or "").rstrip("/"), flags=re.IGNORECASE)
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()


def namespaced_name(server: str, tool: str) -> str:
    return f"{_NS_PREFIX}{_slug(server)}__{tool}"


def _server_prefix(server: str) -> str:
    return f"{_NS_PREFIX}{_slug(server)}__"


def _resolve_session_id() -> str:
    """Per-run session id for scoped 'Trust for session' (contract).

    Resolved ONCE from the active run context (ShadowRuntime.execute() sets it
    from the run's ``execution_id`` via mcp_run_ctx) — NEVER an LLM-supplied
    kwarg. Best-effort: empty string when no run context is active (the gate
    then falls back to non-scoped behaviour, never errors).
    """
    try:
        from systemu.runtime.mcp_run_ctx import current_mcp_session_id
        return str(current_mcp_session_id() or "")
    except Exception:
        return ""


def _make_handler(vault, server: str, tool: str):
    def _handler(**kwargs) -> Dict[str, Any]:
        from sharing_on.config import Config
        from systemu.runtime.mcp.dispatch import call_mcp_tool  # P0 chokepoint
        cfg = Config.from_env()
        # The LLM passes only the tool's own params; server/tool come from the
        # closure (it cannot retarget another server). session_id is resolved
        # from the active run context (NOT from kwargs) so scoped trust works.
        return call_mcp_tool(server, tool, dict(kwargs),
                             vault=vault, config=cfg,
                             session_id=_resolve_session_id())
    return _handler


def _make_check_fn(vault, server: str, tool: str):
    def _check() -> bool:
        try:
            from systemu.runtime.mcp.connections import is_tool_enabled
            return bool(is_tool_enabled(vault, server, tool))
        except Exception:
            return False  # fail-closed
    return _check


def register_server_tools(vault, server: str, tools: List[Dict[str, Any]]) -> List[str]:
    """Register each enabled tool of ``server`` as a namespaced v2 entry.

    ``tools`` are normalised dicts: {name, description (already sanitised),
    parameters_schema, annotations}. Returns the registered names."""
    names: List[str] = []
    for t in tools:
        tool = str(t.get("name") or "")
        if not tool:
            continue
        full = namespaced_name(server, tool)
        ann = dict(t.get("annotations") or {})
        read_only = bool(ann.get("readOnlyHint"))
        registry.register(
            name=full,
            toolset="mcp",
            schema=dict(t.get("parameters_schema") or {"type": "object"}),
            handler=_make_handler(vault, server, tool),
            check_fn=_make_check_fn(vault, server, tool),
            description=str(t.get("description") or f"MCP tool {tool} on {server}"),
            is_action_tool=not read_only,   # read-only => tier R (ungated)
            max_result_size_chars=50_000,
        )
        names.append(full)
    if names:
        registry.invalidate_check_fn_cache()
        logger.info("[MCP] registered %d namespaced tools for %s", len(names), server)
    return names


def unregister_server_tools(server: str) -> int:
    """Drop all of ``server``'s namespaced tools (disable / lease-revoke)."""
    removed = registry.unregister_prefix(_server_prefix(server))
    if removed:
        registry.invalidate_check_fn_cache()
        logger.info("[MCP] unregistered %d tools for %s", removed, server)
    return removed
