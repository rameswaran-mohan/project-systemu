"""MCP client wrapper (v0.9.36 P2 — real client over the official `mcp` SDK).

mcp_call_tool / mcp_list_tools keep their return envelopes but route through the
SDK-isolated ConnectionManager (systemu/runtime/mcp/sdk/manager.py) instead of
the legacy in-process httpx shim. The transport is resolved from the connections
store (connections.transport_for); a bare URL defaults to streamable-HTTP. All
discovered descriptions are sanitised (untrusted-labelled) before they surface.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from systemu.runtime.tool_registry_v2 import registry

logger = logging.getLogger(__name__)


def parse_servers(server_csv: str) -> List[str]:
    """Parse the comma-separated SYSTEMU_MCP_SERVER_URLS env var.

    Empty string -> []. Whitespace around entries is stripped.
    """
    if not server_csv:
        return []
    return [s.strip() for s in server_csv.split(",") if s.strip()]


def _resolve_env_keys(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a persisted transport spec's credential env-var NAMES
    (``env_keys``) to their current values into ``env`` at call time (v0.9.34
    Bug 8). Secrets are NEVER persisted to the connections store — only their
    names — so this re-resolution from the parent env is what makes a credentialed
    stdio server reconnect. Mirrors the connect-time resolution in
    ``Governor._provision_mcp``. The ``env_keys`` field is consumed (not forwarded
    to the SDK)."""
    import os
    keys = spec.pop("env_keys", None)
    if keys:
        env = dict(spec.get("env") or {})
        for k in keys:
            if k in os.environ:
                env[k] = os.environ[k]
        spec["env"] = env
    return spec


def _resolve_transport(server: str, vault) -> Dict[str, Any]:
    """Transport spec for ``server``. With a vault, use the persisted spec
    (resolving credential env-var names to values); a bare URL (or no vault)
    defaults to streamable-HTTP."""
    if vault is not None:
        try:
            from systemu.runtime.mcp.connections import transport_for
            return _resolve_env_keys(transport_for(vault, server))
        except Exception:
            logger.debug("[MCP] transport_for failed; defaulting to http", exc_info=True)
    return {"transport": "http", "url": (server or "").rstrip("/")}


def _run_async(coro):
    """Run an async manager call from sync code. Uses a private loop when no
    loop is running (the daemon/Settings call sites are sync); falls back to a
    worker thread if a loop is already active (NiceGUI/async contexts)."""
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # A loop is already running — execute the coroutine on a fresh loop in a
    # worker thread so we never re-enter the active loop.
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


def mcp_call_tool(
    *,
    server: str,
    name: str,
    params: Optional[Dict[str, Any]] = None,
    config,
    timeout: float = 30.0,
    vault=None,
) -> Dict[str, Any]:
    """Call ``name`` on the MCP ``server`` with ``params`` via the SDK.

    Returns:
        {"success": True, "response": <server payload>} on success
        {"success": False, "error": str} otherwise

    ``server`` may be a URL (legacy) or a logical id whose transport is resolved
    from the connections store when ``vault`` is supplied. Envelope unchanged so
    the P0 chokepoint, quick_task, and Settings call sites are untouched.
    """
    spec = _resolve_transport(server, vault)
    from systemu.runtime.mcp.sdk.manager import get_manager
    mgr = get_manager()
    return _run_async(mgr.call_tool(server, spec, name, params or {}))


def mcp_list_tools(
    *,
    server: str,
    timeout: float = 15.0,
    vault=None,
) -> Dict[str, Any]:
    """Discover the tools a server offers via the SDK.

    Each entry is ``{"name", "description", "schema"}`` (description SANITISED as
    untrusted external content; schema = the mapped parameters_schema with real
    required[]). Honest failure dict on any error — never raises into Settings.
    """
    spec = _resolve_transport(server, vault)
    from systemu.runtime.mcp.sdk.manager import get_manager
    from systemu.runtime.mcp.sdk.schema_map import sanitize_description
    mgr = get_manager()
    try:
        normalised = _run_async(mgr.list_tools(server, spec))
    except Exception as exc:
        return {"success": False, "error": f"discovery failed: {exc}"}
    tools = [
        {
            "name": t["name"],
            "description": sanitize_description(t.get("description", "")),
            "schema": dict(t.get("parameters_schema") or {}),
            "annotations": dict(t.get("annotations") or {}),
        }
        for t in (normalised or []) if t.get("name")
    ]
    return {"success": True, "tools": tools}


def discover_and_pin(vault, server: str) -> Dict[str, Any]:
    """Discover ``server``'s tools and PIN each tool-def hash in the connections
    store (rug-pull baseline). Descriptions are already sanitised by
    mcp_list_tools. Returns the discovery envelope. The operator still opts in
    per tool (set_tool_enabled) — pinning establishes the trusted baseline so a
    later definition drift is detected on use.

    Consistency note: pin and re-check MUST hash the SAME inputs. This hashes the
    sanitised description (what mcp_list_tools returns) + the mapped schema; the
    dispatch re-check path (connections.check_and_pin_hash) re-derives the
    candidate hash the same way — from a fresh mcp_list_tools + tool_def_hash."""
    from systemu.runtime.mcp.connections import set_tool_hash
    from systemu.runtime.mcp.sdk.schema_map import tool_def_hash
    out = mcp_list_tools(server=server, vault=vault)
    if not out.get("success"):
        return out
    for t in out.get("tools", []):
        h = tool_def_hash(name=t["name"], description=t.get("description", ""),
                          input_schema=t.get("schema") or {})
        set_tool_hash(vault, server, t["name"], h)
    return out


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
    """v0.9.34 P0: route the generic mcp_call_tool through the ONE gated
    chokepoint (allowlist + risk-tiered action gate + output guard). Never
    calls the bare httpx transport directly anymore.

    H3 — per-run session id: the session id is resolved from the active run's
    ExecutionContext at v2-dispatch time via the run-scoped contextvar
    (systemu.runtime.mcp_run_ctx.current_mcp_session_id) set in
    ShadowRuntime.execute(). It is NEVER read from **kwargs (those are the
    LLM-supplied tool params; an LLM-controlled session id would let a prompt
    forge "Trust for session" across runs). Pinned-contracts §"Per-run session
    id": resolve ONCE at dispatch time from ExecutionContext/execution_id, NOT
    an LLM-supplied kwarg. (P2's registry_bridge._make_handler threads the SAME
    id via the same carrier — one source of truth.)"""
    from sharing_on.config import Config
    from systemu.runtime.mcp.dispatch import call_mcp_tool
    from systemu.runtime.mcp_run_ctx import current_mcp_session_id
    cfg = Config.from_env()
    vault = None
    try:
        from systemu.vault.vault import Vault
        vault = Vault(cfg.vault_dir)
    except Exception:
        logger.debug("[MCP] handler vault unresolvable", exc_info=True)
    # Low-fix (wrong-vault): Config.from_env() here is the LAST-RESORT vault, used
    # only because the v2-dispatch seam (sandbox.execute → entry.handler(**params))
    # does not yet thread the run vault into the handler. Where the run vault IS in
    # scope it MUST be threaded through call_mcp_tool(..., vault=) instead of re-
    # deriving from env. Coordinated with P2: when registry_bridge._make_handler
    # lands, prefer threading the sandbox/run vault (analogous to the H3 session-id
    # carrier) so a multi-vault deployment scopes the allowlist + gate to the
    # correct vault; this env fallback is the single-vault default only.
    session_id = str(current_mcp_session_id() or "")
    return call_mcp_tool(
        kwargs.get("server", ""),
        kwargs.get("name", ""),
        kwargs.get("params") or {},
        vault=vault,
        config=cfg,
        session_id=session_id,
    )


def _mcp_check_fn() -> bool:
    """L1 availability: defer to dispatch._mcp_any_enabled (lazy import so this
    module imports cleanly without the dispatch layer at registration time)."""
    try:
        from systemu.runtime.mcp.dispatch import _mcp_any_enabled
        return _mcp_any_enabled()
    except Exception:
        logger.debug("[MCP] check_fn errored — advertise nothing", exc_info=True)
        return False


registry.register(
    name="mcp_call_tool", toolset="mcp",
    schema=_MCP_SCHEMA, handler=_mcp_handler,
    check_fn=_mcp_check_fn,          # v0.9.34 P0 L1 — excluded when nothing enabled
    description=(
        "Call a tool on an external MCP (Model Context Protocol) server via the "
        "official MCP SDK. Requires the server id/URL + tool name. Routed through "
        "the gated chokepoint; descriptions are treated as untrusted."
    ),
    is_action_tool=True,  # an MCP call may mutate external state
    max_result_size_chars=50_000,
)


# ── mcp_search_tools — overflow-discovery affordance handler ───────────────
# Same "advertised-but-undispatchable" class as the original mcp_call_tool gap:
# shadow_runtime advertises `mcp_search_tools` (the lazy affordance shown when
# the per-run exposure budget hides MCP tools), but NOTHING registered a handler,
# so calling it failed with "tool not found" and the overflow-discovery path was
# dead. Register the handler here so it's dispatchable. The exposure-budget logic
# in shadow_runtime already strips/re-injects the catalog entry, so the
# LLM-visible behaviour (shown only on overflow) is unchanged — only dispatch is
# fixed. The budget HIDES tools from the catalog, not from the v2 registry, so a
# matched tool is callable by its namespaced name (or via mcp_call_tool).

_MCP_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string",
                  "description": "Keywords to match against tool names/descriptions."},
    },
    "required": ["query"],
}


def _mcp_search_handler(**kwargs) -> Dict[str, Any]:
    """Search the FULL set of enabled MCP tools by keyword and return matches so
    the agent can call a specific one — by its namespaced name
    ``mcp__<server>__<tool>`` or via ``mcp_call_tool(server, name)``. Read-only
    discovery (never mutates). A blank/empty query lists everything."""
    query = str(kwargs.get("query") or "").strip().lower()
    from sharing_on.config import Config
    cfg = Config.from_env()
    vault = None
    try:
        from systemu.vault.vault import Vault
        vault = Vault(cfg.vault_dir)
    except Exception:
        logger.debug("[MCP] search handler vault unresolvable", exc_info=True)
    if vault is None:
        return {"success": False, "error": "no vault available for MCP tool search"}
    try:
        from systemu.runtime.mcp.connections import get_enabled_grouped
        grouped = get_enabled_grouped(vault)
    except Exception as exc:
        logger.debug("[MCP] search enumerate failed", exc_info=True)
        return {"success": False, "error": f"MCP tool search failed: {exc}"}
    terms = [t for t in query.split() if t]
    matches = []
    for server, tools in (grouped or {}).items():
        for t in (tools or []):
            name = str(t.get("name") or "")
            if not name:
                continue
            desc = str(t.get("description") or "")
            hay = f"{name}\n{desc}".lower()
            if not terms or all(term in hay for term in terms):
                matches.append({
                    "tool": f"mcp__{server}__{name}",
                    "server": server,
                    "name": name,
                    "description": desc,
                })
    matches.sort(key=lambda m: m["tool"])
    _LIMIT = 25
    return {
        "success": True,
        "query": query,
        "count": len(matches),
        "truncated": len(matches) > _LIMIT,
        "matches": matches[:_LIMIT],
    }


registry.register(
    name="mcp_search_tools", toolset="mcp",
    schema=_MCP_SEARCH_SCHEMA, handler=_mcp_search_handler,
    check_fn=_mcp_check_fn,          # only dispatchable when MCP is enabled (L1)
    description=(
        "Search the tools available on connected MCP servers by keyword and find "
        "a specific one to use. Use this when the MCP tool you need is not already "
        "listed (the per-run exposure budget hides the rest). Returns matching "
        "tool names you can then call."
    ),
    is_action_tool=False,  # read-only discovery
    max_result_size_chars=20_000,
)
