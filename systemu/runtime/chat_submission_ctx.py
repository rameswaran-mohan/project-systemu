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


# v0.9.52: same carrier pattern for the active execution_id, so the command-gate
# producer (ToolSandbox._maybe_gate_command) can stamp execution_id into the gate
# decision's context — making a parked command gate RESUMABLE — without threading
# execution_id through execute_tool's signature.
_execution_id_var: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "execution_id", default=None
)


def current_execution_id() -> Optional[str]:
    """Return the active execution_id (or None if not inside a run)."""
    return _execution_id_var.get()


def set_execution_id(value: Optional[str], *, reset_token: Any = None):
    """Set or reset the active execution_id. Returns a token usable to reset."""
    if reset_token is not None:
        _execution_id_var.reset(reset_token)
        return None
    return _execution_id_var.set(value)


# v0.10.21: same carrier pattern for the run's activity_id + shadow_id, so a
# tool/command gate that PARKS on the run's FIRST tool call — before any park-rail
# inside ShadowRuntime.execute has written a resume snapshot — can stamp the resume
# coords straight into the gate decision's context. resume_on_decision then reads
# activity_id/shadow_id from the context (no snapshot dependency), so a chat task
# parked on an iteration-1 approval actually resumes once the operator approves.
_activity_id_var: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "activity_id", default=None
)
_shadow_id_var: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "shadow_id", default=None
)


def current_activity_id() -> Optional[str]:
    """Return the active activity_id (or None if not inside a run)."""
    return _activity_id_var.get()


def set_activity_id(value: Optional[str], *, reset_token: Any = None):
    """Set or reset the active activity_id. Returns a token usable to reset."""
    if reset_token is not None:
        _activity_id_var.reset(reset_token)
        return None
    return _activity_id_var.set(value)


def current_shadow_id() -> Optional[str]:
    """Return the active shadow_id (or None if not inside a run)."""
    return _shadow_id_var.get()


def set_shadow_id(value: Optional[str], *, reset_token: Any = None):
    """Set or reset the active shadow_id. Returns a token usable to reset."""
    if reset_token is not None:
        _shadow_id_var.reset(reset_token)
        return None
    return _shadow_id_var.set(value)
