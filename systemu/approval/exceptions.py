"""Exceptions for the v0.8.0 operator-decision queue (Pattern 1 from
the 2026-05-26 architecture audit)."""

from typing import List, Optional


class PendingOperatorDecision(Exception):
    """Raised by ``notify_user`` (queue mode) when the caller's question has
    not yet been answered by an operator.

    Callers (typically CLI commands invoked by the dashboard JobManager
    without a TTY) should catch this exception and exit cleanly with a
    "waiting for operator" message. The decision is persisted in the
    OperatorDecisionQueue; when the operator clicks an action button on
    the dashboard /insights page, the queue is updated and the next
    re-attempt of the command will see the resolved choice and proceed.
    """

    def __init__(
        self,
        decision_id: str,
        dedup_key: str,
        options: List[str],
        message: Optional[str] = None,
    ):
        self.decision_id = decision_id
        self.dedup_key = dedup_key
        self.options = list(options)
        msg = (
            message
            or f"Operator decision pending (id={decision_id}). "
            f"Open the dashboard /insights page and click one of: {', '.join(options)}."
        )
        super().__init__(msg)
