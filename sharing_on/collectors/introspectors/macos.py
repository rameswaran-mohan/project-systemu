"""macOS UI Introspector.

Uses Apple's Accessibility API (AXUIElement) via pyobjc to resolve UI
components at specific screen coordinates.
"""

import logging

from sharing_on.collectors.introspectors.base import BaseUIIntrospector

logger = logging.getLogger(__name__)

class MacOSUIIntrospector(BaseUIIntrospector):
    """
    macOS-specific introspector.
    """
    name = "ui_introspect_macos"

    def _introspect_task(self, coord_dict: dict) -> None:
        """Query the macOS Accessibility tree at X, Y."""
        x = coord_dict.get("x", 0)
        y = coord_dict.get("y", 0)
        btn = coord_dict.get("button", "left")

        try:
            import ApplicationServices
            # macOS Accessibility API hit-test logic goes here for Phase 2/3.
            # We would use AXUIElementCreateSystemWide() and AXUIElementHitTest()
            
            # For now, emit an enriched click with "macOS Native" label
            self._emit_enriched_click(x, y, btn, "macOS Native (Hit-Test Pending)")
        except Exception as e:
            logger.debug(f"macOS Introspection failed: {e}")
            self._emit_enriched_click(x, y, btn, "Unknown")
