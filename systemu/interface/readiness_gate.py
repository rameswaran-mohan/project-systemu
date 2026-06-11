"""Inbox gate for tasks parked by the Stage-3.5 readiness gate (Wave 1.2).

Until now, a task whose required tools weren't deployed+enabled parked as
``waiting_on_tools`` with only a log line and a chat-status string — on a
fresh install (where NOTHING is enabled yet, by Gate-3 design) that is the
DEFAULT first-run experience, and the operator had no path forward short of
discovering the Tools page on their own.

``ensure_tools_blocked_gate`` posts ONE unified Inbox card naming the
blocking tools; "Enable & run" resolves through the same executor chain
(``resolve_gate`` → the canonical ``tools_enable`` verb) every other surface
uses, and the heal sweep re-runs the parked task once tools come ready.

Import-light and NiceGUI-free (callable from pipelines).  Enqueued with
``policy=None`` ALWAYS: enabling LLM-forged tools is Gate 3 — it must never
auto-execute, even under Bypass mode.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def ensure_tools_blocked_gate(vault, activity, not_ready_tools) -> str:
    """Return the pending decision id for ``activity``'s readiness gate,
    enqueueing it on demand when missing.

    Idempotent twice over (mirrors ``ensure_scroll_gate``): pre-check on the
    dedup key ``tools_blocked:<activity_id>``, plus the queue's own pending-row
    dedup.  Best-effort by contract — callers wrap in try/except so a gate
    failure can never break the park itself.
    """
    from systemu.interface.command.gate import GateDescriptor
    from systemu.interface.command.inbox import InboxQueue

    queue = InboxQueue(vault)
    act_id = getattr(activity, "id", "") or ""
    dedup = f"tools_blocked:{act_id}"
    for dec_id, descriptor in queue.list_descriptors():
        if getattr(descriptor, "dedup", "") == dedup:
            return dec_id

    descriptor = GateDescriptor.from_blocked_tools(activity, not_ready_tools)
    return queue.enqueue(
        descriptor,
        gate_type="tools_blocked",
        policy=None,   # Gate 3 — never auto-execute (see module docstring)
        context_extras={
            "tool_ids": [getattr(t, "id", "") for t in (not_ready_tools or [])
                         if getattr(t, "id", "")],
            "activity_id": act_id,
        },
    )
