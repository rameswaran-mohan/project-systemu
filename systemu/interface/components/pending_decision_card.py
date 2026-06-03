"""v0.8.22 (C): inline chat-thread card for a pending operator decision.

Reuses ``systemu.interface.pages.insights.render_decision_card`` primitives so
there's exactly one source of truth for R3 question rendering — eliminates the
v0.8.21 hint-shape bug class as a regression risk.
"""
from __future__ import annotations

from typing import Any, Dict


def build_pending_decision_card(decision_dict: Dict[str, Any], queue, on_resolved=None) -> None:
    """Render an inline pending-decision card in the chat thread.

    ``decision_dict`` is the ``OperatorDecision.to_dict()`` shape — title, body,
    options, context, id, dedup_key. The chat page should call this for each
    visible chat-history entry whose status == 'pending_decision' and whose
    matching OperatorDecision is still pending.
    """
    from systemu.interface.pages.insights import render_decision_card
    # render_decision_card expects a `card` dict; OperatorDecision.to_dict() is
    # already that shape (id/title/body/options/context/dedup_key/status).
    render_decision_card(decision_dict, queue, on_resolved or (lambda: None))
