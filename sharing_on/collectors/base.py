"""Base collector interface — all collectors inherit from this."""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from typing import Optional

from sharing_on.events.models import CaptureEvent
from sharing_on.events.store import EventStore

logger = logging.getLogger(__name__)


class BaseCollector(ABC):
    """Abstract base for all activity collectors.

    Subclasses implement `_collect_loop()` which runs in a dedicated thread.
    Use `self.emit(event)` to send captured events to the store.
    Check `self._running` to know when to stop.
    """

    name: str = "base"

    def __init__(self, event_store: EventStore):
        self._store = event_store
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._error: Optional[str] = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def error(self) -> Optional[str]:
        return self._error

    def start(self) -> None:
        """Start the collector in a background thread."""
        if self._running:
            return
        self._running = True
        self._error = None
        self._thread = threading.Thread(
            target=self._safe_collect_loop,
            daemon=True,
            name=f"collector-{self.name}",
        )
        self._thread.start()
        logger.info(f"Collector '{self.name}' started")

    def stop(self) -> None:
        """Signal the collector to stop and wait for thread completion."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info(f"Collector '{self.name}' stopped")

    def emit(self, event: CaptureEvent) -> None:
        """Send a captured event to the event store (thread-safe)."""
        self._store.put(event)

    def _safe_collect_loop(self) -> None:
        """Wrapper around _collect_loop with error handling."""
        try:
            self._collect_loop()
        except Exception as e:
            self._error = str(e)
            logger.error(f"Collector '{self.name}' crashed: {e}", exc_info=True)
        finally:
            self._running = False

    @abstractmethod
    def _collect_loop(self) -> None:
        """Main collection loop — runs in a background thread.

        Implementations should:
        1. Loop while `self._running` is True
        2. Capture activity
        3. Call `self.emit(event)` for each captured event
        4. Sleep between polls
        """
        ...
