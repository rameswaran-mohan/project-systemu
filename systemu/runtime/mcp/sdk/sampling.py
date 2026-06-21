"""Parent-LLM-bridge core for MCP sampling (and the deferred web_act bridge, Task #11).

A connected MCP server may issue ``sampling/createMessage`` to ask the CLIENT
for an LLM completion (no server-side key). systemu answers it by routing the
request through its OWN ``llm_router`` — systemu controls the model (tier), the
operator can deny via the existing gate pattern, and NO api key ever reaches the
server or any subprocess.

This module imports NOTHING from the ``mcp`` SDK. It speaks plain dicts shaped
like MCP ``CreateMessageRequestParams`` in / ``CreateMessageResult`` out, so the
exact same function backs the web_act parent-LLM-bridge (Task #11): the act-loop
hands its messages here instead of calling ``llm_call_json`` inside a subprocess.
The MCP adapter (transports.py) is the only place that touches SDK types.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# v0.9.38 (review HIGH): a sampling request is SERVER-CONTROLLED. Clamp the cost
# levers so a malicious/compromised server can't make systemu spend unbounded
# output tokens (or push an unbounded prompt) on its own key/budget. These are
# systemu-owned ceilings, applied AFTER the operator gate (the operator still
# sees the requested counts in the redacted summary).
MCP_SAMPLING_MAX_TOKENS = 4096
MCP_SAMPLING_MAX_PROMPT_CHARS = 200_000


def _as_int(value: Any, default: int) -> int:
    """Coerce a server-supplied numeric field, never raising (a non-numeric
    ``maxTokens`` like ``"abc"`` would otherwise crash the bridge)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# Indirection so tests can monkeypatch the network call. The real symbol is the
# async llm_router.llm_call; we resolve it lazily to avoid an import cycle.
async def _llm_call(*, tier, system, user, config, **kw):  # pragma: no cover - thin shim
    from systemu.core.llm_router import llm_call
    return await llm_call(tier, system, user, config, **kw)


def _flatten_text(content: Any) -> str:
    """An MCP message ``content`` is ``{"type":"text","text":...}`` or a list of
    such blocks (image/audio blocks are dropped — text-only bridge for now)."""
    if isinstance(content, dict):
        return str(content.get("text", "")) if content.get("type") == "text" else ""
    if isinstance(content, list):
        return "\n".join(_flatten_text(b) for b in content).strip()
    if isinstance(content, str):
        return content
    return ""


def _messages_to_user_block(messages: List[Dict[str, Any]]) -> str:
    """Collapse the server's conversation into a single user block. systemu's
    llm_call takes one system + one user string; we preserve roles inline so the
    model sees the turn structure without us inventing a multi-turn API here."""
    lines: List[str] = []
    for m in messages or []:
        role = str(m.get("role", "user"))
        text = _flatten_text(m.get("content"))
        if text:
            lines.append(f"[{role}] {text}")
    return "\n".join(lines)


def sampling_summary(req: Dict[str, Any], *, server_id: str, session_id: str,
                     tier: int) -> Dict[str, Any]:
    """A redacted, secret-free summary of a sampling request for the operator
    gate card + the per-call ledger. Deliberately omits systemPrompt and all
    message TEXT — only counts/sizes + the scope coords."""
    return {
        "kind": "sampling",
        "server_id": server_id,
        "session_id": session_id,
        "tier": tier,
        "message_count": len(req.get("messages") or []),
        "max_tokens": _as_int(req.get("maxTokens"), 0),
    }


def route_sampling_request(
    req: Dict[str, Any],
    *,
    config: Any,
    tier: int = 2,
    on_gate: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> Dict[str, Any]:
    """Route an MCP sampling-request dict through systemu's llm_router.

    Parameters
    ----------
    req:    A dict shaped like MCP ``CreateMessageRequestParams``
            (``messages``, optional ``systemPrompt``, ``maxTokens``, ``temperature``).
    config: The runtime Config forwarded to llm_router (carries systemu's keys —
            and they STAY here, never reach the server).
    tier:   systemu's tier choice (model choice is the CLIENT's, not the server's).
    on_gate: Operator-gate hook. Called with a REDACTED summary (never prompt text
            or secrets); return False ⇒ the operator denied ⇒ raise PermissionError.
            This pure core defaults ``on_gate=None`` ⇒ allow, so the bridge stays
            policy-free and reusable (web_act). The PRODUCTION wiring (Task 2b /
            H9) NEVER passes None: the manager injects a gate-backed ``on_gate``
            that defaults to ASKING the operator, on the floor, so BYPASS still
            asks. A None default here is a TEST/library affordance, not the
            production default.

    Returns a dict shaped like MCP ``CreateMessageResult``.
    """
    # H8 / pinned-contracts: do NOT import the PRIVATE llm_router._run_coroutine.
    # Reuse the ONE shared sync loop-runner on the MCP path — client._run_async
    # (the same wrapper P2's connect_and_discover_sync uses). The landed P2 tree
    # exposes it at systemu.runtime.mcp.client (NOT sdk.client) — single adapt-point.
    from systemu.runtime.mcp.client import _run_async

    if on_gate is not None:
        allowed = on_gate({
            "kind": "sampling",
            "tier": tier,
            "message_count": len(req.get("messages") or []),
            "max_tokens": _as_int(req.get("maxTokens"), 0),
        })
        if not allowed:
            raise PermissionError("operator denied the sampling request")

    system = str(req.get("systemPrompt") or "")
    user = _messages_to_user_block(req.get("messages") or [])
    if len(user) > MCP_SAMPLING_MAX_PROMPT_CHARS:
        user = user[:MCP_SAMPLING_MAX_PROMPT_CHARS]
    # Clamp the server-requested output ceiling to systemu's own max (HIGH).
    max_tokens = max(1, min(_as_int(req.get("maxTokens"), 1024), MCP_SAMPLING_MAX_TOKENS))
    _temp = req.get("temperature")
    try:
        temperature = 0.3 if _temp is None else float(_temp)
    except (TypeError, ValueError):
        temperature = 0.3

    resp = _run_async(_llm_call(
        tier=tier, system=system, user=user, config=config,
        temperature=temperature, max_tokens=max_tokens,
    ))
    text = resp.get("content", "")
    if not isinstance(text, str):
        text = str(text)
    return {
        "role": "assistant",
        "content": {"type": "text", "text": text},
        "model": resp.get("model", ""),
        "stopReason": "endTurn",
    }


def web_act_bridge(messages: List[Dict[str, Any]], *, config: Any, tier: int = 2) -> str:
    """Task #11 reuse: drive the web_act planning LLM through the SAME parent
    bridge as MCP sampling, so no key ever enters the browser subprocess.
    Returns the assistant text. (Adapter only — the act-loop owns JSON parsing.)"""
    result = route_sampling_request(
        {"messages": messages, "maxTokens": 512, "temperature": 0.1},
        config=config, tier=tier,
    )
    return result["content"]["text"]
