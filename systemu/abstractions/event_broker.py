"""IEventBroker — backend-agnostic interface for real-time event distribution.

Implementations:
  MemoryEventBroker  — wraps the current in-memory EventBus (local, single-process)
  SqliteEventBroker  — events table + polling (local, cross-process)  [Phase 3]
  RedisEventBroker   — Redis Streams (production, cross-machine)       [Phase 4]

All implementations must be thread-safe.  Callbacks registered via subscribe()
MUST NOT block — they run synchronously on the publishing thread.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class IEventBroker(Protocol):
    """Publish events; subscribe to the live stream; gate on user approvals."""

    # ── Core publish / subscribe ──────────────────────────────────────────────

    def publish(self, event: Dict[str, Any]) -> None:
        """Publish event to all subscribers and append to the history buffer.

        event dict should have: ts, level, category, message, context.
        Non-blocking — callbacks must not block.
        """
        ...

    def subscribe(
        self,
        callback: Callable[[Dict[str, Any]], None],
        *,
        replay: bool = True,
    ) -> Callable[[], None]:
        """Register callback for all future events.

        When replay=True, immediately delivers buffered history to the callback.
        Returns an unsubscribe callable.
        """
        ...

    def get_buffer(self) -> List[Dict[str, Any]]:
        """Thread-safe snapshot of recent event history."""
        ...

    # ── Convenience publishers ────────────────────────────────────────────────

    def publish_supervisor(
        self,
        message: str,
        *,
        level: str = "INFO",
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Publish a supervisor-sourced event (category='supervisor')."""
        ...

    def publish_shadow(
        self,
        message: str,
        shadow_id: str,
        execution_id: str,
        *,
        level: str = "INFO",
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Publish a shadow-execution event (category='shadow')."""
        ...

    # ── Approval gate ─────────────────────────────────────────────────────────

    def request_approval(
        self,
        request_id: str,
        title: str,
        message: str,
        options: List[str],
        *,
        context: Optional[Dict[str, Any]] = None,
        timeout_s: float = 120.0,
        default: str = "",
    ) -> str:
        """Block the caller's thread until the UI resolves the approval or times out.

        Publishes an 'approval' category event; UI calls resolve_approval() to unblock.
        Returns the chosen option string, or default on timeout.
        """
        ...

    def resolve_approval(self, request_id: str, choice: str) -> bool:
        """Called by the UI to resolve a pending approval.  Returns True if found."""
        ...

    def has_pending_approval(self, request_id: str) -> bool:
        """True if the given approval is still awaiting resolution."""
        ...

    def list_pending_approvals(self) -> List[str]:
        """Return all currently pending approval request_ids."""
        ...
