"""Base UI Introspector."""

import logging
import queue
from abc import abstractmethod
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any

from sharing_on.collectors.base import BaseCollector
from sharing_on.events.models import CaptureEvent, EventAction, EventCategory
from sharing_on.events.store import EventStore

logger = logging.getLogger(__name__)

class BaseUIIntrospector(BaseCollector):
    """
    Abstract base class for platform-specific UI Introspection.
    Subscribes to coordinates from a queue and resolves the underlying UI element.
    """
    name = "base_ui_introspect"

    def __init__(self, event_store: EventStore, coordinate_queue: queue.Queue):
        super().__init__(event_store)
        self._coord_queue = coordinate_queue
        # Thread pool to prevent blocking on slow OS accessibility queries
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix=f"{self.name}-pool")

    def _collect_loop(self) -> None:
        """Poll the queue and dispatch introspection tasks."""
        self._on_loop_start()

        while self._running:
            try:
                coord_event = self._coord_queue.get(timeout=0.5)
                if coord_event is None:
                    break
                
                # We do not use the thread pool loop inside uiautomation any more here directly,
                # let _introspect_task handle platform specific thread constraints.
                self._executor.submit(self._introspect_task, coord_event)
            except queue.Empty:
                pass
            except Exception as e:
                logger.debug(f"Queue error in introspector: {e}")

    def _on_loop_start(self) -> None:
        """Hook for platform-specific loop initialization."""
        pass

    @abstractmethod
    def _introspect_task(self, coord_dict: dict) -> None:
        """The actual task run by the thread pool. Must be implemented by subclass."""
        pass

    def stop(self) -> None:
        self._running = False
        self._coord_queue.put(None)
        self._executor.shutdown(wait=False)
        super().stop()
        
    def _emit_enriched_click(self, x: int, y: int, btn: str, app_name: str, window_title: str = "Unknown") -> None:
        """Fallback emit when specific element metadata cannot be found."""
        from datetime import datetime, timezone
        if window_title == "Unknown":
            window_title = app_name
            
        self.emit(CaptureEvent(
            category=EventCategory.INTERACTION,
            action=EventAction.MOUSE_CLICK,
            timestamp=datetime.now(timezone.utc),
            application=app_name,
            window_title=window_title,
            data={
                "x": x,
                "y": y,
                "button": btn,
                "element_name": "Unknown",
                "control_type": "Unknown",
            }
        ))
