"""v0.8.22 (C): contextvars-based carrier for chat_submission_id.

ShadowRuntime.execute() sets the ContextVar at the top of a run; the three R3
producers (notify_user / request_credential / request_choice) read it to thread
chat_submission_id into OperatorDecision.context — without requiring signature
changes to any of those producers.
"""
from __future__ import annotations

import contextvars
from typing import Optional, Any

_chat_submission_id_var: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "chat_submission_id", default=None
)


def current_chat_submission_id() -> Optional[str]:
    """Return the active chat_submission_id (or None if not in a chat-tied run)."""
    return _chat_submission_id_var.get()


def set_chat_submission_id(value: Optional[str], *, reset_token: Any = None):
    """Set or reset the chat_submission_id. Returns a token usable to reset."""
    if reset_token is not None:
        _chat_submission_id_var.reset(reset_token)
        return None
    return _chat_submission_id_var.set(value)
