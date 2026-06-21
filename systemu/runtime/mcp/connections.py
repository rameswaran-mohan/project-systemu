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
            return {"servers": [], "enabled": [], "transports": {},
                    "hashes": {}, "servers_meta": {}}
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            # P0 shape — DO NOT alter (enabled entries keep description/schema/annotations).
            "servers": [s for s in (data.get("servers") or []) if isinstance(s, str)],
            "enabled": [e for e in (data.get("enabled") or []) if isinstance(e, dict)],
            # P2 additions.
            "transports": dict(data.get("transports") or {}),
            "hashes": dict(data.get("hashes") or {}),
            "servers_meta": dict(data.get("servers_meta") or {}),
        }
    except Exception:
        logger.debug("[MCP] connections state unreadable", exc_info=True)
        return {"servers": [], "enabled": [], "transports": {},
                "hashes": {}, "servers_meta": {}}


def _save(vault, state: Dict[str, Any]) -> None:
    path = _path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def add_server(vault, url: str, *, transport: Optional[dict] = None) -> None:
    """Register a server id/URL with an optional transport spec.

    A bare URL (no transport arg) defaults to streamable-HTTP so legacy URL-only
    servers keep working. The transport spec persists in the ``transports`` store
    keyed by the server id; the enabled-entry / per-tool shape is untouched.
    """
    url = (url or "").strip().rstrip("/")
    if not url:
        return
    state = get_state(vault)
    if url not in state["servers"]:
        state["servers"].append(url)
    # Default transport for a bare URL is streamable-HTTP (legacy parity).
    if transport is None:
        transport = {"transport": "http", "url": url}
    state.setdefault("transports", {})[url] = dict(transport)
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
                     description: str = "", schema: Optional[dict] = None,
                     annotations: Optional[dict] = None) -> None:
    """Enable (persisting display metadata + MCP annotations) or disable one
    connector tool.

    ``annotations`` carries the MCP tool hints the action gate reads
    (``readOnlyHint`` / ``destructiveHint``). Absent ⇒ persisted as ``{}`` so
    the dispatch layer treats it as destructive (fail-closed, spec §3.3 L3).
    """
    server = (server or "").rstrip("/")
    state = get_state(vault)
    state["enabled"] = [e for e in state["enabled"]
                        if not (e.get("server") == server and e.get("name") == name)]
    if enabled:
        state["enabled"].append({
            "server": server, "name": name,
            "description": description or f"MCP tool {name} on {server}",
            "schema": dict(schema or {}),
            "annotations": dict(annotations or {}),
        })
    _save(vault, state)


def enabled_tools(vault) -> List[Dict[str, Any]]:
    """The persisted, operator-enabled connector tools (quick-lane surface)."""
    return list(get_state(vault)["enabled"])


def get_enabled_meta(vault, server: str, name: str) -> Optional[Dict[str, Any]]:
    """Return the persisted enabled-tool entry for (server, name), or None.

    The entry carries ``description`` / ``schema`` / ``annotations``. The
    dispatch action gate reads ``annotations`` to pick the risk tier. Defensive:
    a broken vault yields None, never an exception.
    """
    server = (server or "").rstrip("/")
    for e in get_state(vault)["enabled"]:
        if e.get("server") == server and e.get("name") == name:
            entry = dict(e)
            entry.setdefault("annotations", {})
            return entry
    return None


# ── v0.9.36 P2 additions (transport specs, grouped view, server-meta, hashes) ──


def transport_for(vault, server: str) -> Dict[str, Any]:
    """Return the transport spec for ``server``. Bare URLs that predate the
    transport store default to streamable-HTTP so legacy servers keep working."""
    server = (server or "").rstrip("/")
    spec = get_state(vault).get("transports", {}).get(server)
    if isinstance(spec, dict) and spec.get("transport"):
        return dict(spec)
    return {"transport": "http", "url": server}


def set_transport(vault, server: str, spec: Dict[str, Any]) -> None:
    """Persist the transport (reconnect) spec for ``server`` so the stateless
    call path reloads the real recipe via ``transport_for`` instead of the
    ``http://<server_id>`` fallback (v0.9.34 Bug 8).

    SECURITY: callers MUST pass credential env-var NAMES (``env_keys``), never
    resolved secret VALUES — the connections store is plaintext on disk. Values
    are re-resolved from the parent env at call time (client._resolve_transport).
    """
    server = (server or "").rstrip("/")
    if not server:
        return
    state = get_state(vault)
    state.setdefault("transports", {})[server] = dict(spec or {})
    _save(vault, state)


def get_enabled_grouped(vault) -> Dict[str, List[Dict[str, Any]]]:
    """Operator-enabled tools GROUPED BY SERVER (catalog/budget/Settings input).

    NOTE: distinct from P0's per-tool ``get_enabled_meta(vault, server, name)``.
    This is the contract-pinned grouped accessor (contract B1) — it returns
    ``{server: [entry, ...]}`` where each entry keeps P0's shape
    ``{server, name, description, schema, annotations}``. Never reuse the name
    ``get_enabled_meta`` for this view."""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for e in get_state(vault)["enabled"]:
        grouped.setdefault(e.get("server", ""), []).append(dict(e))
    return grouped


def set_server_meta(vault, server: str, *, label: str, transport: str,
                    connected: bool) -> None:
    """Persist per-server metadata (P3 re-attach / arbiter ctx need it).
    ``transport`` here is the transport KIND string (e.g. "stdio"/"http"/"sse"),
    not the full spec dict (that lives in the ``transports`` store)."""
    server = (server or "").rstrip("/")
    state = get_state(vault)
    state.setdefault("servers_meta", {})[server] = {
        "label": str(label or server),
        "transport": str(transport or ""),
        "connected": bool(connected),
    }
    _save(vault, state)


def is_server_connected(vault, server: str) -> bool:
    """True if the last connect attempt for ``server`` succeeded (P3 re-attach)."""
    server = (server or "").rstrip("/")
    meta = get_state(vault).get("servers_meta", {}).get(server) or {}
    return bool(meta.get("connected"))


def set_tool_hash(vault, server: str, name: str, def_hash: str) -> None:
    server = (server or "").rstrip("/")
    state = get_state(vault)
    state.setdefault("hashes", {})[f"{server}\x00{name}"] = def_hash
    _save(vault, state)


def get_tool_hash(vault, server: str, name: str) -> Optional[str]:
    server = (server or "").rstrip("/")
    return get_state(vault).get("hashes", {}).get(f"{server}\x00{name}")


def check_and_pin_hash(vault, server: str, name: str, def_hash: str) -> bool:
    """Rug-pull guard. Returns True if the tool def is unchanged (or first-seen,
    which pins it). On a CHANGED hash returns False AND disables the tool so it
    must be re-approved — no silent definition drift."""
    pinned = get_tool_hash(vault, server, name)
    if pinned is None:
        set_tool_hash(vault, server, name, def_hash)
        return True
    if pinned == def_hash:
        return True
    # Drift detected: disable + leave the new hash UNPINNED (re-approval re-pins).
    set_tool_enabled(vault, server, name, False)
    logger.warning("[MCP] rug-pull: %s.%s definition changed — disabled, re-approval required",
                   server, name)
    return False


def env_autotrust_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """THE ONE canonical reader for SYSTEMU_MCP_ENV_AUTOTRUST (contract).

    Default ON: env-declared servers are grandfathered (server-trusted + tools
    enabled). New Settings/runtime servers always use per-tool opt-in regardless.

    Truthiness rule (align ALL readers — P0 dispatch + config delegate to THIS):
    only an explicit ``"false"``/``"0"``/``"no"``/``"off"`` turns it OFF; the
    UNSET case AND the EMPTY-STRING case (`''`) both mean ON. Never re-implement
    this parse elsewhere — import and call this function."""
    raw = (env or os.environ).get("SYSTEMU_MCP_ENV_AUTOTRUST", "")
    return str(raw).strip().lower() not in {"false", "0", "no", "off"}
