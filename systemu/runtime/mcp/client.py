"""v0.9.5 L6 MCP client wrapper.

Lightweight HTTP wrapper for calling external MCP servers. No OAuth flow
in v0.9.5 — operators bring their own server URLs. OAuth-protected
servers (with the full Hermes mcp_oauth_manager pattern) are deferred
to v0.9.6.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from systemu.runtime.tool_registry_v2 import registry

logger = logging.getLogger(__name__)


def parse_servers(server_csv: str) -> List[str]:
    """Parse the comma-separated SYSTEMU_MCP_SERVER_URLS env var.

    Empty string -> []. Whitespace around entries is stripped.
    """
    if not server_csv:
        return []
    return [s.strip() for s in server_csv.split(",") if s.strip()]


def mcp_call_tool(
    *,
    server: str,
    name: str,
    params: Optional[Dict[str, Any]] = None,
    config,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """Call ``name`` on an MCP server at ``server`` with ``params``.

    Returns:
        {"success": True, "response": <server JSON>} on 2xx
        {"success": False, "error": str} otherwise
    """
    url = f"{server.rstrip('/')}/tools/call"
    payload = {"name": name, "arguments": params or {}}
    try:
        resp = httpx.post(url, json=payload, timeout=timeout)
    except Exception as exc:
        logger.warning("[MCP] %s: connection failed: %s", server, exc)
        return {"success": False, "error": f"connection failed: {exc}"}

    if resp.status_code >= 400:
        return {
            "success": False,
            "error": f"HTTP {resp.status_code}: {getattr(resp, 'text', '')[:200]}",
        }

    try:
        body = resp.json()
    except Exception as exc:
        return {"success": False, "error": f"response not JSON: {exc}"}

    return {"success": True, "response": body}


# ── Tool registration ─────────────────────────────────────────────────

_MCP_SCHEMA = {
    "type": "object",
    "properties": {
        "server": {"type": "string", "description": "MCP server base URL (e.g. http://localhost:8080)."},
        "name": {"type": "string", "description": "Tool name on the server to invoke."},
        "params": {"type": "object", "description": "Arguments passed to the tool."},
    },
    "required": ["server", "name"],
}


def _mcp_handler(**kwargs) -> Dict[str, Any]:
    from sharing_on.config import Config
    cfg = Config.from_env()
    return mcp_call_tool(
        server=kwargs.get("server", ""),
        name=kwargs.get("name", ""),
        params=kwargs.get("params") or {},
        config=cfg,
    )


registry.register(
    name="mcp_call_tool", toolset="mcp",
    schema=_MCP_SCHEMA, handler=_mcp_handler,
    description=(
        "Call a tool on an external MCP (Model Context Protocol) server. "
        "Requires the server URL + tool name. OAuth-protected servers are "
        "not supported in v0.9.5 — that lands in v0.9.6."
    ),
    is_action_tool=True,  # an MCP call may mutate external state
    max_result_size_chars=50_000,
)
