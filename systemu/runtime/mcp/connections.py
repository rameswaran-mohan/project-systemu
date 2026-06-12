"""W9.3 — the MCP connections store: servers + per-tool enablement.

Gate-3 parity for connectors: an MCP tool is OFF until the operator enables
it, exactly like a forged vault tool. Enablement captures the tool's
description/schema at enable time, so the quick lane builds its index from
PERSISTED metadata — no network calls at prompt-build.

State lives in ``<vault>/connections/mcp.json``:

    {"servers": ["http://..."],
     "enabled": [{"server": "...", "name": "...",
                  "description": "...", "schema": {...}}]}

Servers may also come from the read-only ``SYSTEMU_MCP_SERVER_URLS`` env
(v0.9.5 semantics) — merged by :func:`all_servers`, never written back.
Every function is defensive: a broken vault yields empty state, never an
exception (the page shell and the quick lane must not die on a bad file).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)


def _path(vault) -> Path:
    return Path(vault.root) / "connections" / "mcp.json"


def get_state(vault) -> Dict[str, Any]:
    try:
        path = _path(vault)
        if not path.exists():
            return {"servers": [], "enabled": []}
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            "servers": [s for s in (data.get("servers") or []) if isinstance(s, str)],
            "enabled": [e for e in (data.get("enabled") or []) if isinstance(e, dict)],
        }
    except Exception:
        logger.debug("[MCP] connections state unreadable", exc_info=True)
        return {"servers": [], "enabled": []}


def _save(vault, state: Dict[str, Any]) -> None:
    path = _path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def add_server(vault, url: str) -> None:
    url = (url or "").strip().rstrip("/")
    if not url:
        return
    state = get_state(vault)
    if url not in state["servers"]:
        state["servers"].append(url)
        _save(vault, state)


def remove_server(vault, url: str) -> None:
    url = (url or "").strip().rstrip("/")
    state = get_state(vault)
    state["servers"] = [s for s in state["servers"] if s != url]
    # A removed server's tools must vanish from the quick-lane surface too.
    state["enabled"] = [e for e in state["enabled"] if e.get("server") != url]
    _save(vault, state)


def all_servers(vault, env: Optional[Mapping[str, str]] = None) -> List[str]:
    """Vault-managed servers first, then env-declared ones (read-only)."""
    from systemu.runtime.mcp.client import parse_servers
    state = get_state(vault)
    merged = list(state["servers"])
    for url in parse_servers((env or os.environ).get("SYSTEMU_MCP_SERVER_URLS", "")):
        url = url.rstrip("/")
        if url not in merged:
            merged.append(url)
    return merged


def is_tool_enabled(vault, server: str, name: str) -> bool:
    server = (server or "").rstrip("/")
    return any(e.get("server") == server and e.get("name") == name
               for e in get_state(vault)["enabled"])


def set_tool_enabled(vault, server: str, name: str, enabled: bool, *,
                     description: str = "", schema: Optional[dict] = None) -> None:
    """Enable (persisting display metadata) or disable one connector tool."""
    server = (server or "").rstrip("/")
    state = get_state(vault)
    state["enabled"] = [e for e in state["enabled"]
                        if not (e.get("server") == server and e.get("name") == name)]
    if enabled:
        state["enabled"].append({
            "server": server, "name": name,
            "description": description or f"MCP tool {name} on {server}",
            "schema": dict(schema or {}),
        })
    _save(vault, state)


def enabled_tools(vault) -> List[Dict[str, Any]]:
    """The persisted, operator-enabled connector tools (quick-lane surface)."""
    return list(get_state(vault)["enabled"])
