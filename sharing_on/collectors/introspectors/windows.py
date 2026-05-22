"""Windows UI Introspector — resolves screen coordinates to Native UI Elements.

Uses `uiautomation` library to query the Windows Accessibility API.
Ensures we run introspection in a thread pool to avoid hanging the main app on slow apps.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timezone
from typing import Optional, Dict

import uiautomation as auto

from sharing_on.collectors.introspectors.base import BaseUIIntrospector
from sharing_on.events.models import CaptureEvent, EventAction, EventCategory

logger = logging.getLogger(__name__)

class WindowsUIIntrospector(BaseUIIntrospector):
    """
    Windows-specific introspector.
    """
    name = "ui_introspect_windows"

    def _on_loop_start(self) -> None:
        """Called by the base class poll loop thread initialization."""
        # The base poll loop doesn't strictly need COM initialization since the 
        # auto module handles some of this, but the task dispatch does.
        auto.SetGlobalSearchTimeout(1.0) 

    def _introspect_task(self, coord_dict: dict) -> None:
        """Wrapper to ensure COM initialization in the worker thread."""
        with auto.UIAutomationInitializerInThread():
            self._introspect_element_at(coord_dict)

    def _introspect_element_at(self, coord_dict: dict) -> None:
        """Query the Windows UIAutomation tree at X, Y."""
        x = coord_dict.get("x", 0)
        y = coord_dict.get("y", 0)
        btn = coord_dict.get("button", "left")
        
        try:
            # Try once with a short but reasonable timeout
            element = auto.ControlFromPoint(x, y)
            
            # Even if we don't find the specific element, try to get the top level window 
            # so we at least know which app was clicked.
            app_name = "Unknown"
            window_title = "Unknown"
            
            try:
                # Standard Windows API to get window handle at point
                import ctypes
                hwnd = ctypes.windll.user32.WindowFromPoint(ctypes.wintypes.POINT(x, y))
                if hwnd:
                    win_element = auto.ControlFromHandle(hwnd)
                    if win_element:
                        top_window = win_element.GetTopLevelControl()
                        app_name = top_window.Name if top_window else win_element.Name
                        window_title = app_name
            except Exception:
                pass

            if element:
                name = element.Name
                control_type = element.ControlTypeName
                value = ""
                
                try:
                    if hasattr(element, "GetValuePattern"):
                        pattern = element.GetValuePattern()
                        if pattern:
                            value = pattern.Value
                except Exception:
                    pass
                
                # Walk up to find the application/window name if not already found
                if app_name == "Unknown":
                    top_window = element.GetTopLevelControl()
                    app_name = top_window.Name if top_window else "Unknown"
                    window_title = app_name
                
                # Security: Redact Passwords
                if control_type.lower() == "password" or "password" in name.lower():
                    value = "[REDACTED]"
                    name = "[REDACTED]"

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
                        "element_name": name,
                        "control_type": control_type,
                        "value": value
                    }
                ))
            else:
                # Emit a generic click but with at least the app name found via HWND
                self._emit_enriched_click(x, y, btn, app_name)
            
        except Exception as e:
            logger.debug(f"UIAutomation hit an error at {x},{y}: {e}")
            self._emit_enriched_click(x, y, btn, "Unknown")

