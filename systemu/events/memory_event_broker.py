"""MemoryEventBroker — IEventBroker adapter around the in-memory EventBus.

Zero behaviour change.  Delegates all calls to the EventBus singleton so
call sites using IEventBroker are decoupled from the concrete EventBus class.

When Phase 3 (SqliteEventBroker) or Phase 4 (RedisEventBroker) land, this
class is retired — no call sites change.

Usage:
    from systemu.interface.event_bus import EventBus
    from systemu.events.memory_event_broker import MemoryEventBroker

    broker: IEventBroker = MemoryEventBroker(EventBus.get())
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional


class MemoryEventBroker:
    """IEventBroker implementation backed by the in-memory EventBus singleton."""

    def __init__(self, event_bus: Any) -> None:
        """
        Args:
            event_bus: An EventBus instance (or any object matching the EventBus API).
        """
        self._bus = event_bus

    # ── Core publish / subscribe ──────────────────────────────────────────────

    def publish(self, event: Dict[str, Any]) -> None:
        self._bus.publish(event)

    def subscribe(
        self,
        callback: Callable[[Dict[str, Any]], None],
        *,
        replay: bool = True,
    ) -> Callable[[], None]:
        return self._bus.subscribe(callback, replay=replay)

    def get_buffer(self) -> List[Dict[str, Any]]:
        return self._bus.get_buffer()

    # ── Convenience publishers ────────────────────────────────────────────────

    def publish_supervisor(
        self,
        message: str,
        *,
        level: str = "INFO",
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._bus.publish_supervisor(message, level=level, context=context)

    def publish_shadow(
        self,
        message: str,
        shadow_id: str,
        execution_id: str,
        *,
        level: str = "INFO",
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._bus.publish_shadow(
            message, shadow_id, execution_id, level=level, context=context
        )

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
        return self._bus.request_approval(
            request_id, title, message, options,
            context=context, timeout_s=timeout_s, default=default,
        )

    def resolve_approval(self, request_id: str, choice: str) -> bool:
        return self._bus.resolve_approval(request_id, choice)

    def has_pending_approval(self, request_id: str) -> bool:
        return self._bus.has_pending_approval(request_id)

    def list_pending_approvals(self) -> List[str]:
        return self._bus.list_pending_approvals()
