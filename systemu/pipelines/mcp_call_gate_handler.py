"""Dispatcher handler for ``mcp:<server>:<tool>`` operator decisions (v0.9.34 P0).

The MCP action gate (dispatch._gate_mcp_call) posts a four-way decision:
Deny / Approve once / Trust this tool for the session / Always allow. This
handler runs when the operator resolves it:

  * "Always allow"                    -> persist the (server, tool) signature to
                                         the CommandApprovalStore so future runs
                                         skip the gate for that tool.
  * "Trust this tool for the session" -> persist a session-scoped trust key so
                                         re-prompts are suppressed for this exact
                                         (server, tool) within THIS run only.
  * "Approve once"                    -> no persistence; the waiting lane re-reads
                                         the choice via get_resolved_choice and
                                         runs this one time (one-shot bypass).
  * "Deny"                            -> no persistence; lane sees Deny (fail-closed).

Registered into the decision_dispatcher bootstrap so dispatch() routes ``mcp:*``
here even when nothing else imported us (mirrors command_gate_handler.py).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _handle_resolved_mcp_call(decision, choice, config, vault) -> None:
    norm = (choice or "").strip().lower()
    ctx = getattr(decision, "context", None) or {}
    server = ctx.get("server", "") or ""
    tool = ctx.get("tool", "") or ""
    session_id = ctx.get("session_id", "") or ""

    # Recover (server, tool) from the dedup key if context didn't round-trip.
    if not (server and tool):
        # dedup_key shape: mcp:<server>:<tool> — split off the leading "mcp:"
        # then rsplit ONCE on ":" so a server URL containing ":" (the port) is
        # preserved.
        rest = (getattr(decision, "dedup_key", "") or "").partition(":")[2]
        server_part, _, tool_part = rest.rpartition(":")
        server = server or server_part
        tool = tool or tool_part

    from pathlib import Path as _Path
    from systemu.runtime.command_approvals import (
        get_default_store, init_default_store, mcp_signature, mcp_session_key)
    store = get_default_store() or init_default_store(_Path("data"))

    if norm == "always allow":
        sig = mcp_signature(server, tool)
        store.approve(sig, command=f"mcp:{server}:{tool}")
        logger.info("[McpGate] persisted Always-allow for %s:%s", server, tool)
        return
    if norm == "trust this tool for the session":
        # Low-fix (empty session_id): if no run id was resolved (session_id ==
        # ""), a session key would be the SAME hash for EVERY run with no id —
        # i.e. it would leak across runs. Treat an empty session id as
        # approve-once: do NOT persist any session trust; the waiting lane still
        # runs this one call via the one-shot resolved-choice bypass.
        if not (session_id or "").strip():
            logger.warning("[McpGate] 'Trust for session' with empty session_id "
                           "for %s:%s — treating as approve-once (no persist)",
                           server, tool)
            return
        skey = mcp_session_key(server, tool, session_id)
        store.trust_session(skey, server=server, tool=tool, session_id=session_id)
        logger.info("[McpGate] session-trusted %s:%s (run %s)",
                    server, tool, session_id)
        return
    logger.info("[McpGate] resolved %r — no persistence (one-shot/deny)", choice)


from systemu.approval.decision_dispatcher import register as _register_dispatch
_register_dispatch("mcp", _handle_resolved_mcp_call)
