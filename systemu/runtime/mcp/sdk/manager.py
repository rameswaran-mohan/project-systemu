"""v0.9.36 P2 — ConnectionManager: process-local MCP connection orchestration.

Policy (spec §3.1, §3.7, MRTR alignment):
  * REMOTE (http/sse) -> STATELESS REISSUE: open a fresh session per logical
    call/round-trip, carry state in the payload (requestState), never a
    daemon-wide live socket.
  * STDIO -> a live session SCOPED TO THE RUN/PROCESS that spawned it (a
    subprocess can't be made stateless). Cached per (server, canonical-spec);
    idempotent connect; reconstructed on resume.
  * Lazy, bounded timeouts, NEVER connect at import. disconnect_all() at run end.
  * connect_and_discover(server_id, spec) is the ONE connect+discover seam P3
    calls: SSRF/DNS + TLS precheck (remote) -> open_session -> list_tools,
    returning a fixed {connected, oauth_required, authorize_url, error, tools}
    envelope. connect_and_discover_sync wraps it via client._run_async (the ONE
    shared sync runner).
  * Sampling callback slot exists (set_sampling_callback) but is left None in P2;
    P4 fills it. Elicitation callback is injected by Task 13.

All SDK use goes through transports.open_session — this module never imports the
SDK directly. Returns the SAME envelope shape the legacy client used so client.py
can keep its public contract.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from systemu.runtime.mcp.sdk import transports
from systemu.runtime.mcp.sdk.schema_map import (
    mcp_schema_to_parameters, _extract_annotations,
)

logger = logging.getLogger(__name__)

_REMOTE_TRANSPORTS = {"http", "streamable-http", "sse"}


def _spec_key(spec: Dict[str, Any]) -> str:
    import json
    return json.dumps(spec, sort_keys=True, default=str)


def _ssrf_precheck(spec: Dict[str, Any],
                   allowed_hosts: Optional[set] = None) -> tuple:
    """DNS-resolve a remote spec's host and REFUSE link-local / loopback /
    private / cloud-metadata targets unless the host is explicitly allow-listed
    (H5, SECURITY §SSRF). Returns ``(ok: bool, reason: str)``.

    P2 enforces the resolve-and-reject; the allow-list source (HarnessPolicy
    ``allowed_mcp_hosts``) is owned by P3 — passed in by the caller when present,
    defaults to empty here (fail-closed on private ranges)."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    allowed = {h.lower() for h in (allowed_hosts or set())}
    url = str(spec.get("url") or "")
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return (False, "no host in spec url")
    if host in allowed:
        return (True, "allow-listed host")
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception as exc:
        return (False, f"DNS resolution failed: {exc}")
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        # 169.254.169.254 / fd00:ec2::254 cloud-metadata is link-local already.
        if (ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return (False, f"host {host} resolves to non-public address {addr}")
    return (True, "public address")


def _normalise_tool(t: Any) -> Dict[str, Any]:
    """SDK Tool object -> plain dict (mapped schema + annotations).

    Field names match the confirmed surface (mcp==1.26.0: inputSchema,
    annotations.readOnlyHint/destructiveHint)."""
    input_schema = getattr(t, "inputSchema", None)
    if input_schema is None:
        input_schema = getattr(t, "input_schema", None)
    # v0.9.38 (review LOW): tolerate BOTH SDK-object and dict-shaped annotations
    # via the shared extractor. The live SDK hands a Pydantic object, but a
    # dict-shaped annotations (HTTP-JSON / tests) previously yielded {} because
    # this used getattr-only — silently dropping readOnlyHint mis-tiers a
    # read-only tool as an action tool. _extract_annotations handles both shapes
    # and uses the single _ANNOTATION_KEYS source of truth.
    annotations: Dict[str, Any] = _extract_annotations(getattr(t, "annotations", None))
    return {
        "name": str(getattr(t, "name", "") or ""),
        "description": str(getattr(t, "description", "") or ""),
        "parameters_schema": mcp_schema_to_parameters(
            dict(input_schema) if isinstance(input_schema, dict) else input_schema),
        "annotations": annotations,
    }


def _result_to_envelope(result: Any) -> Dict[str, Any]:
    """SDK call result -> {"success", "response"} / {"success": False, "error"}."""
    is_error = bool(getattr(result, "isError", False))
    parts: List[str] = []
    structured = getattr(result, "structuredContent", None)
    for p in (getattr(result, "content", None) or []):
        txt = getattr(p, "text", None)
        if txt is not None:
            parts.append(str(txt))
    body: Any = structured if structured is not None else {"content": parts}
    if is_error:
        return {"success": False, "error": "; ".join(parts) or "tool error"}
    return {"success": True, "response": body}


class ConnectionManager:
    """Process-local. One instance per process (or per run for stdio scoping)."""

    def __init__(self, *, default_timeout: float = 30.0) -> None:
        self._default_timeout = default_timeout
        # Cached live stdio sessions are NOT held across awaits here (the SDK
        # session is an async-cm bound to a task scope); instead stdio reuses
        # the spawn per logical call within a run. We record specs we've seen so
        # connect is idempotent and disconnect_all is a clean no-op surface.
        self._known_stdio: Dict[str, Dict[str, Any]] = {}
        self._elicitation_callback = None  # injected by Task 13
        self._sampling_callback = None     # slot exists from P2 (left None); P4 fills it

    def _is_stateless(self, spec: Dict[str, Any]) -> bool:
        return str(spec.get("transport") or "").lower() in _REMOTE_TRANSPORTS

    def set_elicitation_callback(self, cb) -> None:
        self._elicitation_callback = cb

    def build_elicitation_callback(self):
        """Return an async client-elicitation callback that routes a server's
        elicitation/create through the P1 structured-input surface. Fail-closed:
        any error (headless, no queue, P1 absent) => decline (never fabricate).

        Stores the callback so list_tools/call_tool advertise the elicitation
        capability and serve server requests.
        """
        async def _callback(context, params):
            raw_message = getattr(params, "message", "") or getattr(params, "prompt", "")
            requested = (getattr(params, "requestedSchema", None)
                         or getattr(params, "requested_schema", None) or {})
            # H10: the elicitation message is SERVER-SUPPLIED — sanitise it as
            # untrusted external content before it reaches the operator/P1 (same
            # tool-poisoning defence as discovered descriptions).
            from systemu.runtime.mcp.sdk.schema_map import sanitize_description
            message = sanitize_description(str(raw_message or ""))
            try:
                import anyio
                from systemu.runtime import elicitation as _elic
                # P1's resolver is sync (parks/asks/returns); run it off the loop.
                # B6: route through P1's EXPORTED resolve_structured_input.
                out = await anyio.to_thread.run_sync(
                    lambda: _elic.resolve_structured_input(
                        message=message,
                        requested_schema=dict(requested)
                        if isinstance(requested, dict) else {}),
                )
                action = (out or {}).get("action", "decline")
                content = (out or {}).get("content", {})
                return {"action": action, "content": content}
            except Exception as exc:
                logger.warning("[MCP] elicitation routing failed (declining): %s", exc)
                return {"action": "decline"}

        self._elicitation_callback = _callback
        return _callback

    def set_sampling_callback(self, cb) -> None:
        """Contract slot — exists from P2 (left None). P4 fills the sampling
        handler here; P2 never sets it."""
        self._sampling_callback = cb

    async def list_tools(self, server: str, spec: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not self._is_stateless(spec):
            self._known_stdio[_spec_key(spec)] = spec
        try:
            async with transports.open_session(
                spec, elicitation_callback=self._elicitation_callback,
                sampling_callback=self._sampling_callback,
                init_timeout=self._default_timeout,
            ) as session:
                listed = await session.list_tools()
                return [_normalise_tool(t) for t in (listed.tools or [])]
        except Exception as exc:
            logger.warning("[MCP] list_tools(%s) failed: %s", server, exc)
            return []

    async def call_tool(self, server: str, spec: Dict[str, Any], name: str,
                        arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self._is_stateless(spec):
            self._known_stdio[_spec_key(spec)] = spec
        try:
            async with transports.open_session(
                spec, elicitation_callback=self._elicitation_callback,
                sampling_callback=self._sampling_callback,
                init_timeout=self._default_timeout,
            ) as session:
                result = await session.call_tool(name, arguments or {})
                return _result_to_envelope(result)
        except Exception as exc:
            logger.warning("[MCP] call_tool(%s.%s) failed: %s", server, name, exc)
            return {"success": False, "error": f"connection/call failed: {exc}"}

    async def connect_and_discover(self, server_id: str,
                                   spec: Dict[str, Any], *,
                                   allowed_hosts: Optional[set] = None,
                                   require_tls: bool = False) -> Dict[str, Any]:
        """The ONE connect+discover seam P3 calls (contract-pinned).

        Does the SSRF/DNS + TLS precheck (remote transports) THEN opens a session
        and lists tools. Never raises into the caller — returns a fixed-shape
        envelope:

            {"connected": bool, "oauth_required": bool, "authorize_url": str|None,
             "error": str|None, "tools": [normalised_dict, ...]}

        ``allowed_hosts`` (operator allowlist) is threaded into the SSRF check so
        an allow-listed private/loopback host CAN connect (review MEDIUM). When
        ``require_tls`` is True (the production runtime-connect path passes
        ``policy.mcp_require_tls``), remote transports must be HTTPS / an allowed
        host (review HIGH — the TLS gate previously had no production caller).
        Existing callers default ``require_tls=False`` so behaviour is unchanged.

        ``oauth_required``/``authorize_url`` stay False/None in P2 (the OAuth
        URL-mode handoff is P4); the keys exist so P3/P4 are thin consumers.
        """
        result: Dict[str, Any] = {
            "connected": False, "oauth_required": False,
            "authorize_url": None, "error": None, "tools": [],
        }
        # SSRF/DNS + TLS guard for remote transports (H5). stdio has no host.
        if self._is_stateless(spec):
            if require_tls:
                try:
                    from systemu.runtime.mcp.sdk.remote_policy import enforce_tls
                    enforce_tls(spec.get("url") or "", allowed_hosts=allowed_hosts)
                except Exception as exc:
                    result["error"] = f"blocked by TLS policy: {exc}"
                    return result
            ok, why = _ssrf_precheck(spec, allowed_hosts=allowed_hosts)
            if not ok:
                result["error"] = f"blocked by SSRF/DNS guard: {why}"
                return result
        if not self._is_stateless(spec):
            self._known_stdio[_spec_key(spec)] = spec
        try:
            async with transports.open_session(
                spec, elicitation_callback=self._elicitation_callback,
                sampling_callback=self._sampling_callback,
                init_timeout=self._default_timeout,
            ) as session:
                listed = await session.list_tools()
                result["connected"] = True
                result["tools"] = [_normalise_tool(t) for t in (listed.tools or [])]
        except Exception as exc:
            logger.warning("[MCP] connect_and_discover(%s) failed: %s",
                           server_id, exc)
            result["error"] = f"connect/discover failed: {exc}"
        return result

    def connect_and_discover_sync(self, server_id: str,
                                  spec: Dict[str, Any], *,
                                  allowed_hosts: Optional[set] = None,
                                  require_tls: bool = False) -> Dict[str, Any]:
        """Sync wrapper over connect_and_discover for P3's sync materialise.

        Uses the shared sync runner so there is ONE async-bridge across the code
        base. ``client._run_async`` (systemu.runtime.mcp.client) is that runner;
        importing it here keeps the SDK isolation intact (client.py has no SDK
        import). NEVER call a private llm_router runner — contract reuse rule."""
        from systemu.runtime.mcp.client import _run_async
        return _run_async(self.connect_and_discover(
            server_id, spec, allowed_hosts=allowed_hosts, require_tls=require_tls))

    async def _open_remote(self, url: str, *, transport: str = "http") -> Dict[str, Any]:
        """Async opener for a remote MCP endpoint (delegates to the connect+
        discover seam). Kept as the single delegation point so Task 9's policy
        gate (``connect_remote_sync``) enforces TLS + SSRF host policy BEFORE this
        ever runs."""
        spec = {"url": url, "transport": transport}
        return await self.connect_and_discover(url, spec)

    def connect_remote_sync(self, url, *, allowed_hosts, require_tls=True,
                            transport="http"):
        """Policy-gate a remote MCP connection, THEN delegate to the async opener.

        Order is load-bearing: TLS + SSRF host policy are enforced BEFORE any
        network call so a bad URL never opens a socket. Raises
        InsecureTransportError (plaintext) or PermissionError (SSRF host).

        P4 contributes the TLS-enforcement + allowlist-aware host-policy layer;
        the deeper SSRF/DNS resolution lives in P2's connect_and_discover
        (_ssrf_precheck). ``allowed_hosts`` is INJECTED by the caller (read from
        HarnessPolicy.allowed_mcp_hosts where P3 landed it, else ``set()``)."""
        from systemu.runtime.mcp.sdk.remote_policy import enforce_tls, mcp_host_allowed
        if require_tls:
            enforce_tls(url, allowed_hosts=allowed_hosts)
        if not mcp_host_allowed(url, allowed_hosts=allowed_hosts):
            raise PermissionError(
                f"MCP host policy denied {url!r} (loopback/private/metadata host "
                f"not in the allowed-hosts set)")
        # H8 / pinned-contracts: do NOT import the PRIVATE llm_router._run_coroutine.
        # Delegate to the async opener via the ONE shared MCP loop-runner
        # client._run_async (the same wrapper connect_and_discover_sync uses).
        from systemu.runtime.mcp.client import _run_async
        return _run_async(self._open_remote(url, transport=transport))

    async def disconnect_all(self) -> None:
        """No daemon-held sockets in P2 (reissue model); clear the known-spec
        registry so a fresh run reconnects lazily."""
        self._known_stdio.clear()


# Process-wide singleton (lazy — never connects at import).
_manager: Optional[ConnectionManager] = None


def get_manager() -> ConnectionManager:
    global _manager
    if _manager is None:
        _manager = ConnectionManager()
        _manager.build_elicitation_callback()  # advertise elicitation capability
    return _manager


def reset_manager_for_tests() -> None:
    global _manager
    _manager = None
