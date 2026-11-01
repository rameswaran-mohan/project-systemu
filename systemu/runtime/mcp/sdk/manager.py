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
  * R-A11b-1 SSRF: every entry point that opens a remote session -- list_tools,
    call_tool, connect_and_discover (hence _sync, _open_remote,
    connect_remote_sync) -- obtains ONE verdict from egress.admit_url
    (net_safety underneath) before anything is opened, and opens the session
    under it; transports.open_session refuses a remote session without one.

All SDK use goes through transports.open_session — this module never imports the
SDK directly. Returns the SAME envelope shape the legacy client used so client.py
can keep its public contract.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from systemu.runtime.mcp.sdk import transports
from systemu.runtime.mcp.sdk.schema_map import (
    mcp_schema_to_parameters, _extract_annotations, wire_field,
)

logger = logging.getLogger(__name__)

_REMOTE_TRANSPORTS = {"http", "streamable-http", "sse"}


def _spec_key(spec: Dict[str, Any]) -> str:
    import json
    return json.dumps(spec, sort_keys=True, default=str)


def _ssrf_precheck(spec: Dict[str, Any],
                   allowed_hosts: Optional[set] = None) -> tuple:
    """``(ok, reason)`` for a remote spec -- the CANONICAL verdict (R-A11b-1).

    This used to be a private, weaker copy of the guard (it never extracted an
    embedded IPv4, so a name resolving to 2002:a9fe:a9fe:: -- the 6to4 form of
    169.254.169.254 -- was admitted). It is now a thin view over
    :func:`egress.admit_url` (which delegates to ``net_safety``); kept only for
    callers that want the tuple shape."""
    from systemu.runtime.mcp.sdk.egress import admit_url
    verdict = admit_url(str(spec.get("url") or ""), allowed_hosts=allowed_hosts or ())
    if verdict.admitted is True:
        return (True, "allow-listed host" if verdict.via == "allowance" else "public address")
    return (False, verdict.reason)


class ToolListingFailed(RuntimeError):
    """Raised by iterating a FAILED :class:`ToolListing`.

    The listing value is the fence (DEC-32); this exception only makes an honest
    caller that forgot to check ``.ok`` fail loud instead of reading the failure
    as "the server has zero tools"."""

    def __init__(self, listing: "ToolListing") -> None:
        self.listing = listing
        super().__init__(
            f"tools on {listing.server} could not be listed: {listing.reason}")


@dataclass(frozen=True)
class ToolListing:
    """What ``ConnectionManager.list_tools`` returns -- the ONE value that says
    whether a server was listed.

    Two different facts, never collapsed into one shape:

      * ``ok=True``  -- the server answered; ``tools`` is what it offers (which
        may legitimately be empty), ``reason`` is ``None``.
      * ``ok=False`` -- the server could NOT be listed; ``reason`` says why,
        ``tools`` is empty.

    Before this, a failure returned ``[]``, the same value as a healthy server
    with no tools, so every caller reported ``success: True`` for a server that
    never started. Callers check ``type(x) is ToolListing`` and ``x.ok is True``
    before reading ``tools``; a plain list is not a listing.
    """

    server: str
    ok: bool
    tools: Tuple[Dict[str, Any], ...] = ()
    reason: Optional[str] = None

    def __post_init__(self) -> None:
        if type(self.ok) is not bool:
            raise TypeError(f"ToolListing.ok must be a bool, got {type(self.ok).__name__}")
        if type(self.server) is not str:
            raise TypeError("ToolListing.server must be a str")
        if type(self.tools) is not tuple:
            raise TypeError("ToolListing.tools must be a tuple")
        if self.ok:
            if self.reason is not None:
                raise ValueError("a successful listing carries no failure reason")
            return
        if type(self.reason) is not str or not self.reason.strip():
            raise ValueError("a failed listing must say why it failed")
        if self.tools:
            raise ValueError("a failed listing carries no tools")

    @classmethod
    def listed(cls, server: str, tools) -> "ToolListing":
        return cls(server=str(server), ok=True, tools=tuple(tools), reason=None)

    @classmethod
    def failed(cls, server: str, reason: str) -> "ToolListing":
        return cls(server=str(server), ok=False, tools=(), reason=str(reason))

    def __iter__(self):
        if self.ok is not True:
            raise ToolListingFailed(self)
        return iter(self.tools)


def _normalise_tool(t: Any) -> Dict[str, Any]:
    """SDK Tool object -> plain dict (mapped schema + annotations).

    Fields are read by their WIRE names through ``wire_field`` so the same code
    reads an mcp 1.x model (``inputSchema``, ``annotations.readOnlyHint``) and
    an mcp 2.x model (``input_schema``, ``annotations.read_only_hint``)."""
    input_schema = wire_field(t, "inputSchema")
    # v0.9.38 (review LOW): tolerate BOTH SDK-object and dict-shaped annotations
    # via the shared extractor. The live SDK hands a Pydantic object, but a
    # dict-shaped annotations (HTTP-JSON / tests) previously yielded {} because
    # this used getattr-only — silently dropping readOnlyHint mis-tiers a
    # read-only tool as an action tool. _extract_annotations handles both shapes
    # and uses the single _ANNOTATION_KEYS source of truth.
    annotations: Dict[str, Any] = _extract_annotations(wire_field(t, "annotations"))
    return {
        "name": str(wire_field(t, "name", "") or ""),
        "description": str(wire_field(t, "description", "") or ""),
        "parameters_schema": mcp_schema_to_parameters(
            dict(input_schema) if isinstance(input_schema, dict) else input_schema),
        "annotations": annotations,
    }


def _result_to_envelope(result: Any) -> Dict[str, Any]:
    """SDK call result -> {"success", "response"} / {"success": False, "error"}.

    D-MCP2: read by WIRE name (``wire_field``). On mcp 2.x the attributes are
    ``is_error`` / ``structured_content``; a getattr on ``isError`` there is
    always False, which reported every failed tool call as a success."""
    is_error = bool(wire_field(result, "isError", False))
    parts: List[str] = []
    structured = wire_field(result, "structuredContent")
    for p in (wire_field(result, "content") or []):
        txt = wire_field(p, "text")
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

    async def list_tools(self, server: str, spec: Dict[str, Any], *,
                         allowed_hosts=None) -> ToolListing:
        """List ``server``'s tools. Returns a :class:`ToolListing` -- never a bare
        list, never ``[]`` for a failure, and never raises (an ordinary error is
        a failed listing carrying its reason). A remote spec is SSRF-checked
        before anything is opened (R-A11b-1) -- this is the Settings "Discover
        tools" path, which used to have no check at all."""
        verdict = self._admit(spec, allowed_hosts)
        refused = self._refusal(verdict)
        if refused is not None:
            logger.warning("[MCP] list_tools(%s) refused: %s", server, refused)
            return ToolListing.failed(server, refused)
        if not self._is_stateless(spec):
            self._known_stdio[_spec_key(spec)] = spec
        with transports.StdioWitness.for_spec(spec) as witness:
            try:
                async with self._session(spec, witness, verdict) as session:
                    listed = await session.list_tools()
                    return ToolListing.listed(
                        server, [_normalise_tool(t) for t in (listed.tools or [])])
            except Exception as exc:
                reason = self._why(exc, spec, witness)
                logger.warning("[MCP] list_tools(%s) failed: %s", server, reason)
                return ToolListing.failed(server, reason)

    def _admit(self, spec: Dict[str, Any], allowed_hosts=None):
        """THE SSRF verdict for a remote spec (R-A11b-1), or ``None`` for stdio
        (no host). ``allowed_hosts`` defaults to the operator's MCP allowance
        (``HarnessPolicy.allowed_mcp_hosts``) -- never to anything stored where
        the agent can write (DEC-34)."""
        if not self._is_stateless(spec):
            return None
        from systemu.runtime.mcp.sdk import egress
        if allowed_hosts is None:
            allow = egress.operator_allowance()
        else:   # an explicit allowance still counts the env-declared servers (R-2 c)
            allow = tuple(allowed_hosts) + egress.env_server_allowance()
        return egress.admit_url(str(spec.get("url") or ""), allowed_hosts=allow)

    @staticmethod
    def _refusal(verdict: Any) -> Optional[str]:
        """The refusal reason when ``verdict`` does not admit, else ``None``.
        The concrete type is pinned before the value is read (DEC-36)."""
        if verdict is None:
            return None
        from systemu.runtime.mcp.sdk.egress import EgressVerdict
        if type(verdict) is not EgressVerdict:
            return "blocked by SSRF/DNS guard: no verdict was issued for this URL"
        if verdict.admitted is not True:
            return f"blocked by SSRF/DNS guard: {verdict.reason}"
        return None

    def _session(self, spec: Dict[str, Any], witness: Any, egress_verdict: Any = None):
        """The ONE place the manager opens an SDK session."""
        return transports.open_session(
            spec, elicitation_callback=self._elicitation_callback,
            sampling_callback=self._sampling_callback,
            init_timeout=self._default_timeout, witness=witness,
            egress=egress_verdict,
        )

    def _why(self, exc: BaseException, spec: Dict[str, Any], witness: Any) -> str:
        """Defect 2: the innermost meaningful cause, never the task-group str()."""
        from systemu.runtime.mcp.sdk.failure import describe_failure
        return describe_failure(exc, spec, witness=witness,
                                timeout_s=self._default_timeout)

    async def call_tool(self, server: str, spec: Dict[str, Any], name: str,
                        arguments: Optional[Dict[str, Any]] = None, *,
                        allowed_hosts=None) -> Dict[str, Any]:
        """Call ``name`` on ``server``. A remote spec is SSRF-checked before
        anything is opened (R-A11b-1), and the call is made under that verdict
        (pinned to the checked address)."""
        verdict = self._admit(spec, allowed_hosts)
        refused = self._refusal(verdict)
        if refused is not None:
            logger.warning("[MCP] call_tool(%s.%s) refused: %s", server, name, refused)
            return {"success": False, "error": f"connection/call failed: {refused}"}
        if not self._is_stateless(spec):
            self._known_stdio[_spec_key(spec)] = spec
        with transports.StdioWitness.for_spec(spec) as witness:
            try:
                async with self._session(spec, witness, verdict) as session:
                    result = await session.call_tool(name, arguments or {})
                    return _result_to_envelope(result)
            except Exception as exc:
                reason = self._why(exc, spec, witness)
                logger.warning("[MCP] call_tool(%s.%s) failed: %s", server, name, reason)
                return {"success": False, "error": f"connection/call failed: {reason}"}

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
        verdict = self._admit(spec, allowed_hosts)
        refused = self._refusal(verdict)
        if refused is not None:
            result["error"] = refused
            return result
        if not self._is_stateless(spec):
            self._known_stdio[_spec_key(spec)] = spec
        with transports.StdioWitness.for_spec(spec) as witness:
            try:
                async with self._session(spec, witness, verdict) as session:
                    listed = await session.list_tools()
                    result["connected"] = True
                    result["tools"] = [_normalise_tool(t) for t in (listed.tools or [])]
            except Exception as exc:
                reason = self._why(exc, spec, witness)
                logger.warning("[MCP] connect_and_discover(%s) failed: %s",
                               server_id, reason)
                result["connected"] = False
                result["tools"] = []
                result["error"] = f"connect/discover failed: {reason}"
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

    async def _open_remote(self, url: str, *, transport: str = "http",
                           allowed_hosts=None) -> Dict[str, Any]:
        """Async opener for a remote MCP endpoint (delegates to the connect+
        discover seam, which issues the SSRF verdict the connection is made
        under). ``connect_remote_sync`` enforces TLS + host policy BEFORE this
        ever runs and threads its allowance through."""
        spec = {"url": url, "transport": transport}
        return await self.connect_and_discover(url, spec, allowed_hosts=allowed_hosts)

    def connect_remote_sync(self, url, *, allowed_hosts, require_tls=True,
                            transport="http"):
        """Policy-gate a remote MCP connection, THEN delegate to the async opener.

        Order is load-bearing: TLS + SSRF host policy are enforced BEFORE any
        network call so a bad URL never opens a socket. Raises
        InsecureTransportError (plaintext) or PermissionError (SSRF host).

        Both checks are the canonical guard (``remote_policy.mcp_host_allowed``
        delegates to ``egress.admit_url`` / ``net_safety``); connect_and_discover
        then issues the verdict the connection is actually made under.
        ``allowed_hosts`` is INJECTED by the caller (read from
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
        return _run_async(self._open_remote(url, transport=transport,
                                            allowed_hosts=allowed_hosts))

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
