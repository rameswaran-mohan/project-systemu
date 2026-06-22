"""Global input hook collector — records mouse clicks and keystrokes.

Uses `pynput` for capturing global events.
- Minimizes processing inside the callbacks to prevent OS input lag.
- Buffers keystrokes and fires text-entry events when focus is lost or submission happens.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Optional

try:
    from pynput import keyboard, mouse
    _PYNPUT_AVAILABLE = True
except ImportError:
    keyboard = None  # type: ignore[assignment]
    mouse = None     # type: ignore[assignment]
    _PYNPUT_AVAILABLE = False

from sharing_on.collectors.base import BaseCollector
from sharing_on.events.models import CaptureEvent, EventAction, EventCategory
from sharing_on.events.store import EventStore

logger = logging.getLogger(__name__)

class InputHookCollector(BaseCollector):
    name = "input_hook"

    def __init__(self, event_store: EventStore, coord_queue: Optional[queue.Queue] = None):
        if not _PYNPUT_AVAILABLE:
            raise ImportError(
                "pynput is not installed — InputHookCollector is a capture-only feature "
                "and is not available in daemon / Docker mode."
            )
        super().__init__(event_store)
        self._coord_queue = coord_queue
        self._mouse_listener: Optional[mouse.Listener] = None
        self._keyboard_listener: Optional[keyboard.Listener] = None
        
        # Debouncing specific variables
        self._last_click_time = 0.0
        self._click_debounce_ms = 100  # Merge rapid clicks within this window

        # Thread queue for background emitting
        self._event_queue: queue.Queue = queue.Queue()
        self._dispatcher_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._running:
            return
            
        self._running = True
        self._error = None

        # Start the background dispatcher
        self._dispatcher_thread = threading.Thread(
            target=self._dispatch_loop,
            daemon=True,
            name="input-dispatcher"
        )
        self._dispatcher_thread.start()

        # Start listeners
        self._mouse_listener = mouse.Listener(on_click=self._on_click, on_scroll=self._on_scroll)
        self._keyboard_listener = keyboard.Listener(on_press=self._on_keypress)

        self._mouse_listener.start()
        self._keyboard_listener.start()
        
        logger.info(f"Collector '{self.name}' started")

    def stop(self) -> None:
        self._running = False

        if self._mouse_listener:
            self._mouse_listener.stop()
        if self._keyboard_listener:
            self._keyboard_listener.stop()

        self._event_queue.put(None)  # Sentinel value
        if self._dispatcher_thread and self._dispatcher_thread.is_alive():
            self._dispatcher_thread.join(timeout=2.0)
            
        logger.info(f"Collector '{self.name}' stopped")

    def _collect_loop(self) -> None:
        # Overriding base collect loop behavior since pynput is event-driven
        pass

    def _dispatch_loop(self) -> None:
        """Pulls events from queue and emits them to not block pynput callbacks."""
        while self._running or not self._event_queue.empty():
            try:
                event = self._event_queue.get(timeout=0.5)
                if event is None:
                    break
                self.emit(event)
            except queue.Empty:
                pass

    def _on_click(self, x, y, button, pressed) -> None:
        if not pressed:
            return
            
        now = time.time()
        # Debounce to prevent 100 queries a second for spam clickers
        if (now - self._last_click_time) * 1000 < self._click_debounce_ms:
            return
        self._last_click_time = now

        btn_name = str(button).replace("Button.", "")

        # Pushing heavy action to a queue to resolve immediately returning from hook
        ts = datetime.now(timezone.utc)
        
        # We define a custom action string just to trigger introspector later, 
        # or we just rely on standard naming conventions
        # For our architecture, the Orchestrator or the UIIntrospector will attach
        # to THIS class or listen to these events.
        
        event = CaptureEvent(
            category=EventCategory.WINDOW,  # Grouping under window for interaction context
            action=EventAction.WINDOW_FOCUS, # Temporary action, we'll redefine
            timestamp=ts,
            data={
                "type": "mouse_click",
                "x": x,
                "y": y,
                "button": btn_name
            }
        )
        # Update specific action
        event.action = EventAction("interaction") if hasattr(EventAction, "interaction") else EventAction.STEP_MARKER
        # Let's ensure our model works! We actually need to define the action string clearly.
        event.category = EventCategory("interaction") # We'll update the enum later or pass as value
        event.action = EventAction("mouse_click") 
        
        # Using pure string if Enums blow up (we should update the Enums in models.py)
        event.data["original_category"] = "interaction"
        
        self._event_queue.put(event)
        
        # Dispatch to the UI Introspector if wired up
        if self._coord_queue is not None:
            self._coord_queue.put({
                "x": x,
                "y": y,
                "button": btn_name
            })

    def _on_scroll(self, x, y, dx, dy) -> None:
        # We can implement scroll capturing but for now, we keep it simple
        pass
        
    def _on_keypress(self, key) -> None:
        # Avoid saving raw text. Just buffer or ignore for MVP.
        # For now, we can just save special keys (like Enter/Escape) to signify step demarcations
        try:
            if hasattr(key, "name") and key.name in ["enter", "esc", "tab"]:
                ts = datetime.now(timezone.utc)
                event = CaptureEvent(
                    category=EventCategory.MARKER, 
                    action=EventAction.STEP_MARKER,
                    timestamp=ts,
                    data={
                        "type": "key_press",
                        "key": key.name
                    }
                )
                event.data["original_category"] = "interaction"
                self._event_queue.put(event)
        except Exception:
            pass
