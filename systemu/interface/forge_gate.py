"""Inspect-before-forge for tool-forge gates (Phase 5 Slice 3e).

Makes the Inbox the discovery surface for forge gates AND routes approving a
forge gate through the RICH human-code-review dialog (``tools._show_spec_review_
dialog``: spec review → human reads/edits generated code → ``save_approved_code``)
rather than the degraded ``resolve_gate`` one-shot.

The forge gate row does NOT reliably exist: only the activity-extractor
proposed-tool seam (``activity_extractor._queue_forge_notifications`` /
``_maybe_enqueue_forge_gate``) enqueues a ``forge:<id>`` gate (for AUTO-PROPOSED
tools, which the ``resolve_gate`` one-shot owns).  A tool reached via the
registry "Review & Forge" button has no row.  ``ensure_forge_gate`` therefore
enqueues ON DEMAND, idempotently, and CRITICALLY with ``policy=None`` — passing
``load_default_policy()`` would AUTO-EXECUTE under Bypass mode via
``_synthetic_approved`` (inbox.py), re-running ``forge_tool_from_spec`` over the
UNEDITED spec and skipping the human code review the operator just asked for.

Mirrors ``scroll_gate.ensure_scroll_gate`` and
``remediation_card.ensure_recovery_gate`` (both ``policy=None``, never auto-exec).
Import-light: no NiceGUI import here, so ``ensure_forge_gate`` is testable
headless.
"""
from __future__ import annotations


def ensure_forge_gate(vault, tool) -> str:
    """Return the pending decision id for ``tool``'s forge gate, enqueueing it
    on demand when missing.

    Idempotent twice over: we pre-check ``list_descriptors`` for the tool's
    dedup key (``forge:<id>``), and ``OperatorDecisionQueue.post`` itself dedups
    pending rows on dedup_key — double-clicks can never produce two gates.

    NEVER auto-executes: ``policy=None`` skips the gate-mode dial entirely, so
    even under ``SYSTEMU_GATE_MODE=bypass`` this POSTS a pending row for the
    operator to review (they explicitly asked to review the code, so Bypass's
    auto-grant must not run the degraded one-shot here).
    """
    from systemu.interface.command.gate import GateDescriptor
    from systemu.interface.command.inbox import InboxQueue

    queue = InboxQueue(vault)
    tool_id = getattr(tool, "id", "") or (
        tool.get("id", "") if isinstance(tool, dict) else "")
    dedup = f"forge:{tool_id}"
    for dec_id, descriptor in queue.list_descriptors():
        if getattr(descriptor, "dedup", "") == dedup:
            return dec_id

    descriptor = GateDescriptor.from_forge(tool)
    return queue.enqueue(descriptor, gate_type="forge", policy=None)
